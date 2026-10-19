"""规律前置注册的**调度接线**（MU-0 / W06）。

## 为什么需要这个模块（这是全系统最上游的一处断链）

V13 / MU 评审实测发现：`hypothesis.register_prereg()` 在**非测试代码里调用点为 0**。

后果链是一整条：

```
register_prereg 没人调用
  → 假说永远没有 prereg_status
  → evaluate_prereg 只处理 prereg_status ∈ {registered, running, provisional}
  → prereg_ok 永远不可能为 True
  → patterns_runtime.usable_rules() 结构上恒返回 []
  → engine.apply_rules_to_candidates 恒走 mode="off"
  → 界面永远显示「可用规律 0 条」，而且**无论注入多强的真实效应都是 0**
```

这条链解释了三个此前互相矛盾的观测（为什么 `usable=0`、为什么开了 `LOTT_RULE_APPLY=1`
也没反应、为什么 `patterns_runtime` 的注释要写「engine 从不看规律库」）——
它们是同一个原因的不同侧面。

**本模块把这一环接上**：每期调度时，把本轮的维度扫描候选登记成"先于读结果的实验"。

## 三条设计纪律（缺一条就会变成噪声生产机）

1. **只登记 `source == "stat"` 的候选。**
   `patterns_runtime` 硬编码 `source=="llm"` 永不 usable（LLM 产出不可回放），
   若把 LLM 假设也灌进 α 台账，每个用户用自己 key 生成的假设会**稀释所有人的
   检验预算**（U3 §5.5-E）。所以这里只接统计扫描，LLM 侧等"可回放性判据"
   （M3 §7 的 R1–R5）落地后再单独接。

2. **登记 ≠ 可用。** 注册只是把候选钉在先验位置上，让它进入
   150 期冻结期（`FROZEN_ISSUES`）+ 累计 Bonferroni 校正。
   候选可以全部是噪声——这正是前置注册的价值：**噪声会被记录下来并计入分母**，
   而不是被悄悄丢掉或反复重试直到偶然显著。

3. **每期最多登记一批，且必须 ≥ `MIN_BATCH_SIZE`。**
   注册簿的 `n_tested_cumulative` 是**只增不减**的校正分母（R1），
   所以**同一 spec 重复注册必须是幂等的**（`exp_id = sha1(spec)` 保证这一点），
   否则每期重跑会把分母刷爆、把所有候选永久钉死在"不显著"。

## 为什么不登记"全部候选"

`discover()` 返回的 `items` 是**按效应量排序的 top-N**（默认 20 条）。若把 124 档
全登记，分母会立刻膨胀到 124 且每期增加；而 top-N 已经是"值得检验"的合理子集。
必要时可由 `max_register` 控制。

## 预期产出

接线后第一次调度运行，`data/hypotheses.json` 应出现 `prereg_status='registered'`
的记录、`counters.batches` 出现一条批次、`last_issue` 被写入。
**`usable` 仍会是 0**（因为冻结期 150 期 + 校正后仍不显著）——但那是一个
「有分母的 0」，与现在「判据写死导致的 0」在可信度上完全不同。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import config

# 每期最多登记多少条候选（避免分母被单次扫描刷爆）
MAX_REGISTER_PER_ISSUE = 20

# 只登记这些来源（见纪律 1）。当前只有统计扫描被接上。
REGISTRABLE_SOURCES = ("stat",)


def discover_candidates(draws: Sequence[Dict], *,
                        limit: int = MAX_REGISTER_PER_ISSUE) -> List[Dict]:
    """跑维度扫描并取出 top-N 候选（**不做显著性过滤**）。

    为什么不在这里按 p 值过滤：前置注册的意义恰恰是"先登记、后看结果"。
    若只登记已经显著的那些，就退化成"挑选后再检验"——那正是多重检验要找的问题。
    噪声候选照登记，它们会进入分母并被校正掉。
    """
    from . import discovery
    out = discovery.discover(list(draws))
    items = list(out.get("items") or [])
    return items[: max(1, int(limit))]


def candidates_to_hypotheses(items: Sequence[Dict], *, issue: str,
                             evaluate_after_issue: Optional[int] = None) -> List[Dict]:
    """把 `discover()` 的候选条目转成 `register_prereg()` 接受的假说结构。

    `_spec_canonical()` 只读这四处：
      ``action.filter.{key, params, target}`` / ``prereg_test`` / ``prereg_direction``
      / ``prereg_min_samples``
    所以必须严格按这个形状构造，否则 `exp_id` 会退化成一堆空 spec（全部去重成同一条）。
    """
    hyps: List[Dict] = []
    for it in items:
        flt = it.get("filter") if isinstance(it.get("filter"), dict) else {}
        key = str(flt.get("key") or "")
        if not key:
            continue
        quick = it.get("quick") if isinstance(it.get("quick"), dict) else {}
        hyps.append({
            "source": "stat",                     # ← 纪律 1：只有 stat 可登记
            "family": key,
            "statement": str(flt.get("name_zh") or key),
            "issue": issue,
            # 记录登记时的观测（**仅供追溯**，不参与判定：
            # 判定一律由 evaluate_prereg 在冻结期满后用累计口径重算）
            "registered_observation": {
                "n": quick.get("n"),
                "delta": quick.get("delta"),
                "z": quick.get("z"),
                "p_raw": quick.get("p"),
                "p_adjusted": quick.get("p_adjusted"),
                "scan_status": it.get("status"),
            },
            "action": {
                "filter": {
                    "key": key,
                    "params": flt.get("params") if isinstance(flt.get("params"), dict) else {},
                    "target": str(flt.get("target") or "red"),
                }
            },
            "prereg_test": "t",
            "prereg_direction": "two-sided",
            "prereg_min_samples": 100,
        })
        if evaluate_after_issue is not None:
            hyps[-1]["evaluate_after_issue"] = int(evaluate_after_issue)
    return hyps


def _already_registered_for_issue(issue: str) -> bool:
    """该期号是否已经有登记批次（用于避免重复追加 batch 元数据）。"""
    if not issue:
        return False
    try:
        from . import hypothesis
        reg = hypothesis._load_registry(None)                  # noqa: SLF001
        for b in ((reg.get("counters") or {}).get("batches") or []):
            if isinstance(b, dict) and str(b.get("issue") or "") == str(issue):
                return True
    except Exception:  # noqa: BLE001 —— 检查失败时按"未登记"处理（宁可多写一次）
        return False
    return False


def register_scan_candidates(draws: Sequence[Dict], *, issue: str,
                             limit: int = MAX_REGISTER_PER_ISSUE,
                             reason: str = "scheduler: 每期维度扫描候选自动登记"
                             ) -> Dict[str, Any]:
    """调度入口：扫描 → 转假说 → 登记。**任何异常都不得中断调度**。

    返回 `{ok, scanned, registered, deduped, batch_id, judgeable, errors, error}`，
    供调度器打印与测试断言。
    """
    from . import hypothesis

    out: Dict[str, Any] = {"ok": False, "scanned": 0, "registered": 0, "deduped": 0,
                           "batch_id": None, "judgeable": False, "errors": [],
                           "error": None}
    try:
        # 空/过短输入直接短路：`discovery.discover()` 对 0 期会抛
        # `ValueError: 传入的 CTX 期数 0 与 start=200 不一致`。
        # 那不该走异常路径（异常路径是给"真故障"用的，正常空数据不该进日志告警）。
        if not list(draws or []):
            out["ok"] = True
            out["error"] = "no_draws"
            return out
        items = discover_candidates(draws, limit=limit)
        out["scanned"] = len(items)
        if not items:
            out["ok"] = True
            out["error"] = "no_candidates"
            return out

        # 同一期已经登记过 → 直接跳过，**不重复追加 batch 元数据**。
        #
        # 为什么需要这层短路：`register_prereg` 的幂等只作用在 hypothesis 记录上
        # （同 exp_id 计入 deduped），但它**无条件**往 `counters.batches` 追加一条批次记录。
        # 调度器在开奖日会因为手工触发/重启补跑等原因对同一期多次执行，
        # 就会堆出一串相同 batch_id 的噪声（实测同一期出现 3 条）。
        # 不影响判定（判定看的是 hypothesis 上的 batch_id），但会污染注册簿审计记录。
        if _already_registered_for_issue(issue):
            out["ok"] = True
            out["error"] = "already_registered_for_issue"
            return out
        hyps = candidates_to_hypotheses(items, issue=issue)
        if not hyps:
            out["ok"] = True
            out["error"] = "no_valid_hypotheses"
            return out
        res = hypothesis.register_prereg(
            hyps, issue=issue, batch_id=f"scan-{issue}", reason=reason)
        out.update({
            "ok": True,
            "registered": int(res.get("registered") or 0),
            "deduped": int(res.get("deduped") or 0),
            "batch_id": res.get("batch_id"),
            "judgeable": bool(res.get("judgeable")),
            "errors": list(res.get("errors") or []),
        })
        return out
    except Exception as e:  # noqa: BLE001 —— 登记失败只降级，绝不中断调度
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def registry_health() -> Dict[str, Any]:
    """注册簿健康度（给 `/api/registry/status` 与自检用）。

    存在的意义：把"有没有人在写"变成可观测指标。接线若被误删，
    这里的 `never_registered` 会立刻为 True，而不是等到几个月后才发现
    `usable` 一直是 0。
    """
    from . import hypothesis
    try:
        reg = hypothesis._load_registry(None)                  # noqa: SLF001
        counters = reg.get("counters") or {}
        hyps = reg.get("hypotheses") or []
        by_status: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for h in hyps:
            st = str(h.get("prereg_status") or "none")
            by_status[st] = by_status.get(st, 0) + 1
            src = str(h.get("source") or "unknown")
            by_source[src] = by_source.get(src, 0) + 1
        n_registered = sum(v for k, v in by_status.items()
                           if k in ("registered", "running", "provisional"))
        return {
            "ok": True,
            "total": len(hyps),
            "prereg_status": by_status,
            "source": by_source,
            "n_tested_cumulative": int(counters.get("n_tested_cumulative") or 0),
            "last_issue": counters.get("last_issue"),
            "n_batches": len(counters.get("batches") or []),
            # ← 核心告警项：True 表示"没有任何在途实验"，等于验证环节空转
            "never_registered": n_registered == 0,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
