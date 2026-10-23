"""AI 假设生成器（V21 / L1）：LLM 提**可证伪的假设**，系统自动证伪。

## 为什么要重做（docs/analysis/V19_AI_REDESIGN.md §1.1）

旧的 `llm_lab` 要求 LLM 输出 `filters:[{key, params}]`，`key` 必须来自维度表。
于是 LLM 的全部输出空间 = **134 个候选的排列组合** —— 这是搜索，不是推理。
而枚举这 134 档只要 12.8~17 秒（`discover` 实测），比问 LLM 快两个数量级，
且确定性可复现。**用 LLM 做这件事，等于用卡车送一封信。**

更糟的是：`data/llm_claims.json` 的 `claims` 与 `runs` **都是 0** ——
这条通道在生产上从未产出过任何东西。

## 本模块怎么做

把 LLM 的职责从「挑参数」改成「**决定问哪个问题、为什么问、怎么证伪**」：

```
{
  "question":   "第 1 个开奖位置的号码是否比其它位置更稳定？",   ← 自然语言的统计问题
  "test_spec":  {"key": "order_pos", "params": {...}},        ← 必须指向已实现的检验族
  "why":        "摇奖机出球顺序可能与球道物理特性有关（若无机理请直说巧合）",
  "falsifier":  "若前向回测 z 中位数 ≤ 1.0 或置换检验 p ≥ α，则判否"
}
```

**关键约束**：`test_spec.key` 只能取 `discovery.FAMILIES` 里已存在的族，
`params` 只能取该族已声明的参数档 —— **不许 LLM 自己发明统计量**。
那会变成"LLM 编了个看起来像统计的东西"，无法验证，也就失去了证伪的意义。

## 自动证伪三关（缺一不可）

一条假设要进候选池，必须同时通过：

1. **存在性**：`discover` 能跑出这条族（否则是幻觉出来的维度）；
2. **阶段**：`regime` 判定不是 `flipped`（方向反转 = 伪规律）；
3. **前向回测**：`walk_forward_gate` 的 z 中位数 > 门槛（V20 / L0 已实现）。

第 3 关是本次重设计的核心 —— **V19 已证明单靠置换检验会把噪声当信号**。

## 诚实边界（写进返回值，不进代码注释就没人看）

* 本模块**不产出号码**，只产出「值得进一步检验的假设」；
* 即使三关全过，也只是「这个角度值得看」，**不承诺提高中奖率**；
* 三关全不过的假设会**原样保留**并标注失败原因 —— 这比只留成功的更有价值：
  它记录了「我们问过什么、答案是没有」。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from . import discovery

__all__ = [
    "HYPOTHESIS_SCHEMA",
    "build_hypothesis_prompt",
    "validate_hypothesis",
    "falsify_hypothesis",
    "run_hypotheses",
]

#: 允许 LLM 指定的最大假设条数（防止它一次吐 50 条把预算烧光）。
MAX_HYPOTHESES = 8

#: 提示词里给 LLM 的 JSON 骨架（与 `validate_hypothesis` 的校验严格对应）。
HYPOTHESIS_SCHEMA: Dict[str, Any] = {
    "hypotheses": [
        {
            "question": "用一句自然语言描述你要检验的统计问题",
            "test_spec": {"key": "<维度族 key>", "params": {"<参数名>": "<值>"}},
            "why": "为什么这个问题可能成立；若只是直觉/巧合，请直说",
            "falsifier": "什么结果会让你放弃这个假设",
        }
    ],
    "overall": "一句话总结你的思路",
}


def _family_brief() -> List[Dict]:
    """给 LLM 看的维度清单（只给 key/维度/参数档，不给已有结论 —— 避免它抄结论）。"""
    out = []
    for f in discovery.FAMILIES:
        out.append({
            "key": f.get("key"),
            "dim": f.get("dim_zh"),
            "ratio": f.get("ratio"),
            "kind": "distribution_test" if f.get("stat") is not None else "selector",
            "params": (f.get("params") or [])[:4],   # 只给前 4 档，控制提示词体积
        })
    return out


def build_hypothesis_prompt(context: Optional[Dict] = None) -> str:
    """构造「提假设」提示词（V21）。

    与旧 `llm_lab.build_user_prompt` 的**根本区别**：

    * 旧：要求输出 `filters:[{key,params}]` —— LLM 在 134 档里挑组合（搜索）；
    * 新：要求输出 `question + test_spec + why + falsifier` —— LLM 决定问什么问题、
      为什么问、什么结果算否（推理）。

    提示词明确要求：**如果没有机理，就直说是巧合** —— 这是防"事后叙事"的关键，
    与 P2/P7 反复强调的「不要给噪声编故事」一致。
    """
    fams = _family_brief()
    ctx_blob = ""
    if context:
        ctx_blob = ("\n## 已知的统计事实（供参考，不要复述）\n```json\n"
                    + json.dumps(context, ensure_ascii=False)[:2000] + "\n```\n")
    return (
        "## 你的任务\n"
        "你要为「双色球开奖数据是否包含可检验的结构」提出**可证伪的统计假设**。\n"
        "注意：**你不是在选号**，也不是在从我给的维度里挑一个组合交差。\n"
        "你要指出「应该检验哪个问题」，并说明**什么结果会让你放弃它**。\n"
        "\n## 可用检验维度（test_spec.key 只能取这里的值）\n"
        "```json\n" + json.dumps(fams, ensure_ascii=False) + "\n```\n"
        f"{ctx_blob}\n"
        "## 输出要求\n"
        f"- 最多 {MAX_HYPOTHESES} 条假设；\n"
        "- ``question``：一句自然语言的统计问题（不要写成「用 hot 族 w=30 k=6」这种参数描述）；\n"
        "- ``test_spec``：``{key, params}``，key 必须是上面列表里的；params 必须是该族已声明的档位；\n"
        "- ``why``：为什么这个问题可能成立。**若只是直觉或民俗说法，请直说是巧合** ——\n"
        "  给噪声编机理是本项目明确反对的行为；\n"
        "- ``falsifier``：什么结果会让你放弃。必须可操作（例如「前向回测 z ≤ 1」）。\n"
        "\n## 重要约束\n"
        "- 双色球是独立随机事件，**理论上不存在可预测性**。你的假设若被数据否定，\n"
        "  那是最正常、也最有价值的结果（它排除了一个可能性）；\n"
        "- 不要为了「看起来有产出」而把噪声包装成规律；\n"
        "- 如果你认为某个维度根本不值得检验，可以在 overall 里说明，返回空列表也可以。\n"
        "\n输出 JSON：\n```json\n" + json.dumps(HYPOTHESIS_SCHEMA, ensure_ascii=False) + "\n```"
    )


def validate_hypothesis(h: Dict) -> Dict:
    """校验一条假设的结构与「可执行性」，返回 ``{ok, errors, normalized}``。

    ## 为什么校验必须严格

    `test_spec` 是 LLM 唯一的"落地点"。若允许它写不存在的 key 或不在档位表里的
    params，这条假设就**无法被任何代码验证** —— 于是「证伪」变成了一句空话，
    而系统会以为自己在做科学。这正是旧 `llm_lab` 的毛病（LLM 只在既有档位里挑，
    看起来可执行，实际只是搜索）。

    校验三件事：
    1. `key` 必须存在于 `discovery.FAMILY_BY_KEY`；
    2. `params` 必须**完全匹配**该族已声明的某一档（不许自造参数）；
    3. `question`/`falsifier` 非空（没有 falsifier 的假设不可证伪，必须拒）。
    """
    errors: List[str] = []
    h = h or {}
    q = str(h.get("question") or "").strip()
    why = str(h.get("why") or "").strip()
    fal = str(h.get("falsifier") or "").strip()
    spec = h.get("test_spec") if isinstance(h.get("test_spec"), dict) else {}

    if not q:
        errors.append("question 为空（无法知道要检验什么）")
    if not fal:
        errors.append("falsifier 为空（不可证伪的假设不得进入候选池）")

    key = str(spec.get("key") or "").strip()
    fam = discovery.FAMILY_BY_KEY.get(key)
    if not fam:
        errors.append(f"test_spec.key={key!r} 不是已实现的维度族"
                      f"（合法值见 FAMILIES，共 {len(discovery.FAMILY_BY_KEY)} 个）")
        return {"ok": False, "errors": errors, "normalized": None}

    params = spec.get("params") or {}
    matched = None
    for cand in fam.get("params") or []:
        # 参数档必须**完全一致**：多一个键、少一个键、值不同都不算匹配。
        # 这是防「LLM 自造参数」的关键 —— 自造的参数没有对应实现，
        # 跑出来的东西无法归因，等于不可验证。
        if isinstance(cand, dict) and set(cand) == set(params or {}) and \
                all(cand[k] == (params or {}).get(k) for k in cand):
            matched = dict(cand)
            break
    if matched is None:
        errors.append(
            f"test_spec.params={params!r} 不匹配 {key!r} 的任何已声明档位"
            f"（合法档位：{(fam.get('params') or [])[:4]}…）；"
            "自造参数无法被验证，不得使用")
        return {"ok": False, "errors": errors, "normalized": None}

    if errors:
        return {"ok": False, "errors": errors, "normalized": None}
    return {
        "ok": True, "errors": [],
        "normalized": {
            "question": q, "why": why, "falsifier": fal,
            "test_spec": {"key": key, "params": matched},
            "dim_zh": fam.get("dim_zh"), "kind": (
                "distribution_test" if fam.get("stat") is not None else "selector"),
        },
    }


def falsify_hypothesis(draws: Sequence[Dict], hypothesis: Dict,
                       *, alpha: float = 0.05,
                       fast_window: int = 600, top: int = 12) -> Dict:
    """对**一条**假设跑证伪三关，返回结构化结论（永不抛异常）。

    三关（与模块 docstring 对应）：

    1. **存在性**：把该族交给 `discover(families=[key])` 跑一遍，看能否产出记录；
    2. **阶段**：看 `regime.status` 是否 `flipped`（方向反转 ⇒ 伪规律）或 `error`；
    3. **前向回测**：对**该族的祖先方法**做 `walk_forward_check`（V20）。

    ⚠️ 第 3 关的诚实说明：`test_spec` 里的族是"维度"，不是"出号方法"，
    因此**无法直接做前向回测**（前向回测的对象是 `stat:freq` 这类方法）。
    因此第 3 关的语义是「用该维度所属的**方法通道**做前向回测」，
    并在返回值里明确标注 `wf_target`，避免把「方法的回测结果」误读成
    「这个维度的回测结果」。

    这是本模块最容易被误读的地方，所以**必须在返回值里写清楚**。
    """
    out: Dict = {
        "ok": False, "question": hypothesis.get("question"),
        "test_spec": hypothesis.get("test_spec"),
        "gates": {}, "verdict": "unverifiable", "reason_zh": "",
    }
    spec = (hypothesis.get("test_spec") or {})
    key = spec.get("key")
    params = spec.get("params") or {}
    if not key or key not in discovery.FAMILY_BY_KEY:
        out["reason_zh"] = f"test_spec.key={key!r} 不存在，无法证伪（可能是幻觉出来的维度）"
        out["gates"] = {"exists": False, "regime": None, "walk_forward": None}
        return out

    fam = discovery.FAMILY_BY_KEY[key]

    # ---- 第 1 关：存在性（能否真的跑出这个族）----
    try:
        res = discovery.discover(list(draws or []), top=int(top),
                                 families=[key], fast_window=int(fast_window),
                                 use_cache=False)
        recs = res.get("all_quick") or []
        exists = bool(recs)
    except Exception as e:                     # noqa: BLE001
        recs = []
        exists = False
        out["reason_zh"] = f"存在性检验抛异常：{type(e).__name__}: {e}"
    out["gates"]["exists"] = exists
    if not exists:
        out["verdict"] = "not_found"
        out.setdefault("reason_zh", "")
        out["reason_zh"] = out["reason_zh"] or (
            "该维度在本次扫描中没有产出任何记录（可能是数据不足，或该族的参数档全被跳过）")
        # 仍给出该族的最小可检出说明，便于人理解"不是没有，是测不出"
        out["note_zh"] = fam.get("note_zh")
        return out

    # ---- 第 2 关：阶段（是否方向反转/报错）----
    try:
        flt = discovery.make_filter(key, params or None)
        rep = discovery.regime_of_filter(list(draws or []), flt,
                                         n_tested=int(res.get("n_tested") or 0) or None)
        status = rep.get("status")
    except Exception as e:                     # noqa: BLE001
        status = None
        rep = {"status": "error", "reason_zh": f"{type(e).__name__}: {e}"}
    out["gates"]["regime"] = {"status": status,
                              "reason_zh": rep.get("reason_zh"),
                              "n_sig_eras": rep.get("n_sig_eras")}
    if status in ("flipped", "error"):
        out["verdict"] = "regime_rejected"
        out["reason_zh"] = (
            f"阶段判定为 {status}："
            + ("方向反转说明至少一侧是搜索出来的假阳性，禁止使用。" if status == "flipped"
               else "阶段检验执行失败。"))
        return out

    # ---- 第 3 关：前向回测（作用于方法通道，不是该维度本身）----
    wf_target = "stat:freq"
    # 起点必须**按数据长度自适应**：写死 2917 在短样本上会让 n=0、
    # 前向回测直接返回 z=None（第一版就是这样，导致测试里三关只跑了前两关）。
    # 取「留出至少 400 期做前向评估」的起点，且不低于 300（`battle` 的默认下限）。
    _n = len(draws or [])
    _wf_start = max(300, min(2917, _n - 400))
    try:
        from . import walk_forward_gate as _WFG
        wf = _WFG.walk_forward_check(list(draws or []), wf_target,
                                     n_tickets=5, start=_wf_start, color="blue")
    except Exception as e:                     # noqa: BLE001
        wf = {"ok": False, "reason_zh": f"前向回测不可用（{type(e).__name__}）",
              "passed": False, "z": None}
    out["gates"]["walk_forward"] = wf
    out["wf_target"] = wf_target
    out["wf_caveat_zh"] = (
        f"注意：前向回测的对象是**方法通道 {wf_target}**，不是维度 {key} 本身 —— "
        "维度无法直接做前向回测（它是检验规格，不产出号码）。"
        "因此这一关只能说明「该通道能不能向前用」，不能当作该维度的直接证据。")

    out["ok"] = True
    if wf.get("passed"):
        out["verdict"] = "passed_all_gates"
        out["reason_zh"] = (
            f"三关全过：维度可执行、阶段无反转、方法通道前向回测过线"
            f"（z 中位数 {wf.get('z_median')}）。**这不等于该维度有预测力** —— "
            "只是「值得继续看」。")
    else:
        out["verdict"] = "walk_forward_rejected"
        out["reason_zh"] = (
            f"前向回测未过（方法通道 {wf_target}：z 中位数 {wf.get('z_median')}）。"
            "按 V20 双重门槛，不足以称为信号。")
    return out


def run_hypotheses(draws: Sequence[Dict], hypotheses: Sequence[Dict],
                   *, alpha: float = 0.05) -> Dict:
    """批量校验 + 证伪，返回汇总（**保留失败项**，它们同样有价值）。"""
    t0 = time.time()
    items: List[Dict] = []
    for raw in (hypotheses or [])[:MAX_HYPOTHESES]:
        v = validate_hypothesis(raw)
        if not v["ok"]:
            items.append({
                "question": (raw or {}).get("question"),
                "valid": False, "errors": v["errors"],
                "verdict": "invalid_spec",
                "reason_zh": "假设规格不合法，未进入证伪：" + "；".join(v["errors"]),
            })
            continue
        norm = v["normalized"]
        res = falsify_hypothesis(draws, norm, alpha=alpha)
        res["valid"] = True
        res["why"] = norm.get("why")
        res["falsifier"] = norm.get("falsifier")
        res["dim_zh"] = norm.get("dim_zh")
        res["kind"] = norm.get("kind")
        items.append(res)

    from collections import Counter
    tally = Counter(it.get("verdict") for it in items)
    passed = [it for it in items if it.get("verdict") == "passed_all_gates"]
    return {
        "ok": True,
        "n_hypotheses": len(items),
        "n_passed": len(passed),
        "verdict_counts": dict(tally),
        "items": items,
        "elapsed_sec": round(time.time() - t0, 1),
        "note_zh": (
            "本函数**不产出号码**，只产出「值得继续检验的假设」。"
            "三关全过（passed_all_gates）也不承诺提高中奖率 —— "
            "双色球为独立随机事件，被数据否定才是最正常的结果。"
            "失败项同样保留：它们记录了「我们问过什么、答案是没有」。"),
    }
