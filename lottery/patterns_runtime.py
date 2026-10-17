"""规律运行时快照（patterns_runtime）：把「阶段时效 + 维度扫描 + 规律库」压缩成
一份**低延迟**可用的规律清单，供预测引擎在每个请求里直接消费。

为什么需要它
------------
V6 把规律的**检验**做对了（阶段有效性、as-of、搜索成本），但规律的**使用**没接通：

* `engine.predict_next()` 从来不看规律库，也从不调用 `evolution.predict_with_evolution()`；
* LLM 在选号提示里只拿到 A/B 级规律——实测全库 **0 条 A 级、8 条 B 级**，
  于是证据里只能写「无显著规律（回测 p_adj 均>0.05）」。

也就是说：**规律找到了、验证了，但没有一条真的影响出号**。这正是用户第 3、4 条反馈。

本模块负责把三个阶段接起来，并且把**慢的重算**与**快的出号**解耦：

    阶段画像（regime，数百毫秒~数秒）
        + 维度扫描（discovery，约 5 秒）
        + 假说库（hypothesis/evolution）
            ↓  定期/手动刷新（写 data/patterns_runtime.json）
    规律运行时快照（本模块，读一次 <1ms）
            ↓  engine.predict_next() 每个请求直接读
    出号偏置（shadow 记录影响 / active 真正生效）+ 票级「规律引用」证据链

设计约束（诚实口径）
--------------------
1. **未通过验证的规律不得影响出号**。快照里的条目分三档：
   * ``usable``       —— 可用于出号（已验证 + 阶段未过期）；
   * ``monitor``      —— 只展示、只监控（阶段新兴/衰减/陈旧）；
   * ``rejected``     —— 明确不可用（方向反转、A/B 反对）。
2. **默认 shadow 模式**：规律会参与计算并**记录它对本期号码的实际影响**，
   但只有 ``activate=1``（默认由 `LOTT_RULE_APPLY` 开关控制）才真正改变号码。
   这样「规律到底有没有用」变成可观察、可回滚的事实，而不是口号。
3. 快照是**派生数据**，可随时重建；文件缺失时引擎照常出号（不阻塞、不报错）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Sequence

from . import config

SNAPSHOT_FILE = config.DATA_DIR / "patterns_runtime.json"
SNAPSHOT_VERSION = "v7-2"

# 生成快照所需的最小历史期数。低于此数：阶段无法切分、分位数无意义，
# 生成出来的快照只会误导浏览器（本轮踩到：一次 400 期的沙箱回放把快照基准期号
# 写成了 2026399，容器读到后把「有候选」当作真实结论）。
MIN_DRAWS_FOR_SNAPSHOT = 500

# 允许进入出号链路的阶段状态。注意 ``emerging`` 只进 monitor：
# 只有一个阶段显著就拿来改号，等于把「搜索出来的假阳性」直接变成投注。
USABLE_STATUSES = ("persistent",)
MONITOR_STATUSES = ("emerging", "fading", "stale", "reversed", "insufficient")


def _now() -> float:
    return time.time()


def _age_text(ts: Optional[float]) -> str:
    if not ts:
        return "未生成"
    sec = max(0, _now() - float(ts))
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    return f"{int(sec // 86400)} 天前"


def build_snapshot(draws: Sequence[Dict], scan: Optional[Dict] = None,
                   include_registry: bool = True) -> Dict[str, Any]:
    """重算并返回一份新的规律快照（不落盘；落盘用 ``save_snapshot``）。

    内容：
      * ``rules``：每条形如
        ``{id, source, statement, dim:{key,params}, target, regime:{status,label,reason},
           effect, p_adjusted, usable, evidence_text}``
      * ``counts``：usable / monitor / rejected 数量
      * ``base_issue`` / ``n_draws``：快照的数据基准（用于判断是否过期）
    """
    from . import discovery, evolution, hypothesis, regime

    draws = list(draws or [])
    if len(draws) < MIN_DRAWS_FOR_SNAPSHOT:
        raise ValueError(
            f"历史仅 {len(draws)} 期 < {MIN_DRAWS_FOR_SNAPSHOT} 期，不足以生成规律快照"
            f"（阶段切分与分位数会失真）；已拒绝写入，避免污染正在运行的快照")
    if scan is None:
        scan = discovery.discover(draws, top=12)
    rules: List[Dict] = []
    # 1) 假说库（统计 + LLM 工坊注册 + 已验证结构类）
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    for h in hyps:
        act = h.get("action") if isinstance(h.get("action"), dict) else {}
        spec = act.get("filter") if act.get("type") in ("prefer_red_filter", "prefer_blue_filter") else None
        if not spec:
            spec = evolution.hypo_structure_filter(h)
        if not spec:
            continue
        rep = h.get("regime") or {}
        status = rep.get("status") or "noise"
        usable = (status in USABLE_STATUSES) and h.get("status") == "validated"
        rules.append({
            "id": h.get("id"), "source": h.get("source") or "stat",
            "statement": h.get("statement"), "dim": spec,
            "target": (discovery.FAMILY_BY_KEY.get(spec.get("key")) or {}).get("target", "red"),
            "hyp_status": h.get("status"),
            "regime": {"status": status, "label": rep.get("label_zh"),
                       "reason": rep.get("reason_zh"),
                       "effect": rep.get("recent_delta"), "p_adjusted": rep.get("p_adjusted_recent")},
            "usable": bool(usable),
            "tier": "usable" if usable else ("monitor" if status in MONITOR_STATUSES else "rejected"),
            "evidence_text": _evidence_text(h, rep),
        })
    # 2) 维度扫描里「阶段持续有效」的维度——它们是当前数据里唯一有阶段证据的线索
    for it in (scan.get("items") or []):
        conf = "filter"
        st = it.get("status")
        if st not in ("persistent", "emerging", "fading"):
            continue
        f = it.get("filter") or {}
        q = it.get("quick") or {}
        tier = "monitor"          # 扫描候选未经 A/B 验证，一律只监控
        rules.append({
            "id": f"scan:{f.get('key')}:{json.dumps(f.get('params') or {}, sort_keys=True)}",
            "source": "discovery", "statement": f"{f.get('name_zh')} {f.get('params')}",
            "dim": {"key": f.get("key"), "params": f.get("params") or {}},
            "target": f.get("target", "red"), "hyp_status": None,
            "regime": {"status": st, "label": it.get("status_zh"),
                       "reason": it.get("reason_zh"), "effect": q.get("delta"),
                       "p_adjusted": q.get("p_adjusted")},
            "usable": False, "tier": tier,
            "evidence_text": f"{f.get('name_zh')} {f.get('params')}：{it.get('status_zh')}；"
                             f"近窗效应 {q.get('delta')}，BH p={q.get('p_adjusted')}",
        })
    counts = {"usable": sum(1 for r in rules if r["tier"] == "usable"),
              "monitor": sum(1 for r in rules if r["tier"] == "monitor"),
              "rejected": sum(1 for r in rules if r["tier"] == "rejected")}
    return {
        "version": SNAPSHOT_VERSION,
        "generated_at": _now(),
        "generated_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_issue": draws[-1].get("issue") if draws else None,
        "n_draws": len(draws),
        "counts": counts,
        "scan": {"n_tested": scan.get("n_tested"),
                 "expected_false_positives": scan.get("expected_false_positives"),
                 "best_quick_p_adjusted": scan.get("best_quick_p_adjusted"),
                 "verdict": scan.get("verdict")},
        "eras": [e.get("name") for e in regime.build_eras(draws)],
        "rules": rules,
    }


def _evidence_text(h: Dict, rep: Dict) -> str:
    bits = []
    if rep.get("reason_zh"):
        bits.append(rep["reason_zh"])
    if rep.get("recent_delta") is not None:
        bits.append(f"近窗效应 {rep['recent_delta']:+.4f}")
    if rep.get("p_adjusted_recent") is not None:
        bits.append(f"BH p={rep['p_adjusted_recent']:.4g}")
    bits.append(f"假说状态 {h.get('status')}")
    return "；".join(bits)


def validate_snapshot(snap: Optional[Dict], library_max_issue: Optional[str] = None) -> Dict:
    """快照自检：拒绝期号非法、样本过少、与当前库明显不一致的写入。

    为什么要这一层：快照是**跨进程共享的派生状态**（容器读写同一挂载目录），
    任何一次离线脚本/沙箱回放只要误写一次，线上就会读到假结论。
    这里把「有效性」写成代码里的硬校验，而不是靠人记得别写错。
    """
    if not isinstance(snap, dict):
        return {"ok": False, "reason": "快照不是对象"}
    issue = str(snap.get("base_issue") or "")
    if not (len(issue) == 7 and issue.isdigit()):
        return {"ok": False, "reason": f"base_issue 非法：{issue!r}（应为 7 位数字期号）"}
    n = int(snap.get("n_draws") or 0)
    if n < MIN_DRAWS_FOR_SNAPSHOT:
        return {"ok": False, "reason": f"样本期数 {n} < {MIN_DRAWS_FOR_SNAPSHOT}，快照不可信"}
    seq = int(issue[4:])
    if seq > 1600:
        return {"ok": False, "reason": f"期号序号 {seq} 超出自洽范围（双色球每年约 151 期）"}
    if library_max_issue:
        cur = str(library_max_issue)
        if cur.isdigit() and len(cur) == 7:
            try:
                gap = int(cur[4:]) - seq
                if abs(gap) > 30 and cur[:4] == issue[:4]:
                    return {"ok": False,
                            "reason": f"快照基准 {issue} 与当前库最新 {cur} 相差 {abs(gap)} 期，疑似用错数据源生成"}
            except ValueError:
                pass
    return {"ok": True, "reason": "ok"}


def save_snapshot(snap: Dict[str, Any], path=None,
                  library_max_issue: Optional[str] = None) -> Dict:
    p = path or SNAPSHOT_FILE
    check = validate_snapshot(snap, library_max_issue=library_max_issue)
    if not check["ok"]:
        return {"ok": False, "error": f"快照校验未通过：{check['reason']}"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        tmp.replace(p)
    except OSError as e:
        return {"ok": False, "error": f"写入失败：{e}"}
    return {"ok": True, "path": str(p), "counts": snap.get("counts")}


def refresh(draws: Sequence[Dict], path=None) -> Dict:
    """重算并落盘（调度器/手动按钮共用；约 5~15 秒）。"""
    draws = list(draws or [])
    snap = build_snapshot(draws)
    saved = save_snapshot(snap, path=path,
                          library_max_issue=(draws[-1].get("issue") if draws else None))
    return {"ok": bool(saved.get("ok")), **saved, "snapshot": {
        "generated_at_text": snap["generated_at_text"], "base_issue": snap["base_issue"],
        "counts": snap["counts"], "scan": snap["scan"]}}


_CACHE: Dict[str, Any] = {"mtime": None, "data": None}


def load_snapshot(path=None, max_age_draws: int = 3,
                  current_issue: Optional[str] = None) -> Optional[Dict]:
    """读快照（按文件 mtime 缓存，单次 <1ms）。

    过期判定：快照的数据基准（``base_issue``）距今超过 ``max_age_draws`` 期，
    或与调用方给的 ``current_issue`` 不一致时，标记 ``stale=True``
    —— 引擎会据此只做 shadow、并在证据里注明「规律快照过期」。
    文件不存在时返回 None（引擎照常出号）。
    """
    p = path or SNAPSHOT_FILE
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
        data = _CACHE["data"]
    else:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
    check = validate_snapshot(data, library_max_issue=current_issue)
    if not check["ok"]:
        return None
    out = dict(data)
    base = str(data.get("base_issue") or "")
    stale = False
    if current_issue and base:
        try:
            stale = int(str(current_issue)[4:]) - int(base[4:]) > int(max_age_draws)
        except (TypeError, ValueError):
            stale = False
    out["stale"] = stale
    out["age_text"] = _age_text(data.get("generated_at"))
    return out


def apply_enabled() -> bool:
    """规律是否**真正改变**号码（默认关闭 = shadow 模式，只记录影响）。"""
    return os.environ.get("LOTT_RULE_APPLY", "0") == "1"


def usable_rules(snapshot: Optional[Dict]) -> List[Dict]:
    if not snapshot or snapshot.get("stale"):
        return []
    return [r for r in (snapshot.get("rules") or []) if r.get("usable")]


def rules_for_prompt(snapshot: Optional[Dict], limit: int = 8) -> List[Dict]:
    """给 LLM 提示词用的规律清单（含可用与只监控两类，各自标注可否用于出号）。"""
    if not snapshot:
        return []
    rows = []
    for r in (snapshot.get("rules") or [])[:limit]:
        rows.append({"key": r.get("dim", {}).get("key"), "params": r.get("dim", {}).get("params"),
                     "target": r.get("target"), "tier": r.get("tier"),
                     "statement": (r.get("statement") or "")[:90],
                     "regime": (r.get("regime") or {}).get("status"),
                     "effect": (r.get("regime") or {}).get("effect"),
                     "p_adjusted": (r.get("regime") or {}).get("p_adjusted")})
    return rows
