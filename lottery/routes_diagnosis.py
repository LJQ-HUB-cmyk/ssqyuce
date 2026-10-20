"""规律注册簿状态 + ML 诊断域路由（P2 从 api_app 的 V4 域切出）。

## `registry_status` 是「信任可查的 0」的出口

它返回前置注册簿的真实状态：`n_tested_cumulative`（累计检验次数）、
已登记候选、判定结果。W06 之前 `register_prereg()` 零调用，导致
`never_registered=True`、可用规律恒为 0 —— 那个 0 是"判据写死"的假 0。
W06 接上写入侧之后，同一个 0 变成了"有分母、可复核"的真 0。
本端点是唯一能把这两者区分开的地方，**不要改成返回一个漂亮的数字**。

## `ml_diagnosis` 的缓存与后台任务

`shuffled_label_test(n_null=20)` 实测 11–165 秒，必须走缓存 + 后台刷新
（`_kick_ml_diag_bg`），否则一次请求就把 40 线程池的一个 worker 占死。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import config, db
from .bg_tasks import _bg_run
from .jsonutil import jsonable as _jsonable

router = APIRouter()

def _registry_health_for_status() -> Dict:
    """注册簿健康度（不抛异常，失败时返回 ok=False）。"""
    try:
        from . import prereg_scheduler
        return prereg_scheduler.registry_health()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/api/registry/status")
def registry_status(include_scan: bool = Query(False)):
    """V10-10「📋 前置注册」卡：已登记实验的计数 + 每条规则的 prereg 理由 + 累计检验数。

    数据源（只读，**不**跑 ``evaluate_prereg``，判定由调度器唯一写者负责）：
      1. ``hypothesis.load_registry()`` → 每条假说的 ``prereg_*`` 字段 + 库根 counters；
      2. ``patterns_runtime.load_snapshot()`` → 快照里每条规则的 ``prereg`` 子块
         （含 ``reason`` / ``divergence`` / ``n_tested_batch`` / ``n_tested_cumulative``）。
    ``include_scan=True`` 时把快照中来自维度扫描（无 prereg）的规则也列出来并标
    ``prereg_status=None``，默认只列已登记实验，避免把扫描线索误读成"路过注册"。
    """
    from . import hypothesis, patterns_runtime
    reg = hypothesis.load_registry() or {}
    hyps = reg.get("hypotheses") or []
    counters = reg.get("counters") if isinstance(reg.get("counters"), dict) else {}

    items: List[Dict] = []
    for h in hyps:
        if not isinstance(h, dict):
            continue
        status = h.get("prereg_status")
        if status is None and not include_scan:
            continue
        items.append({
            "exp_id": h.get("exp_id") or h.get("id"),
            "hyp_id": h.get("id"),
            "statement": h.get("statement"),
            "source": h.get("source") or "stat",
            "prereg_status": status,
            "prereg_ok": bool(h.get("prereg_ok")),
            "batch_id": h.get("batch_id"),
            "scope": h.get("scope"),
            "p_batch": h.get("p_batch"),
            "p_cumulative": h.get("p_cumulative"),
            "n_tested_batch": h.get("n_tested_batch"),
            "n_tested_cumulative": h.get("n_tested_cumulative"),
            "divergence": bool(h.get("divergence")),
            "reason": h.get("prereg_reason"),
            "evaluate_after_issue": h.get("evaluate_after_issue"),
            "frozen_until_issue": h.get("frozen_until_issue"),
            "hyp_status": h.get("status"),
        })
    items.sort(key=lambda x: (str(x.get("prereg_status") or "zz"), str(x.get("exp_id") or "")))

    counts = {s: 0 for s in hypothesis.PREREG_STATUSES}
    for it in items:
        st = str(it.get("prereg_status") or "")
        if st in counts:
            counts[st] += 1
    judged = sum(counts[s] for s in ("passed", "provisional", "failed", "invalidated"))

    snapshot = patterns_runtime.load_snapshot()
    snap_rules = []
    if snapshot:
        for r in (snapshot.get("rules") or []):
            pre = r.get("prereg") or {}
            if not pre and not include_scan:
                continue
            if not pre and include_scan:
                snap_rules.append({
                    "id": r.get("id"), "source": r.get("source"),
                    "prereg_status": None, "usable": bool(r.get("usable")),
                    "tier": r.get("tier"), "reason": "维度扫描线索，未登记前置实验",
                })
                continue
            snap_rules.append({
                "id": r.get("id"), "source": r.get("source"),
                "prereg_status": pre.get("status"),
                "prereg_ok": bool(pre.get("status") == "passed"),
                "exp_id": pre.get("exp_id"), "scope": pre.get("scope"),
                "p_batch": pre.get("p_batch"), "p_cumulative": pre.get("p_cumulative"),
                "n_tested_batch": pre.get("n_tested_batch"),
                "n_tested_cumulative": pre.get("n_tested_cumulative"),
                "divergence": bool(pre.get("divergence")),
                "reason": pre.get("reason"),
                "usable": bool(r.get("usable")), "tier": r.get("tier"),
            })

    return _jsonable({
        "ok": True,
        "counts": counts,
        "registered": counts.get("registered", 0) + counts.get("running", 0),
        "passed": counts.get("passed", 0),
        "provisional": counts.get("provisional", 0),
        "failed": counts.get("failed", 0),
        "invalidated": counts.get("invalidated", 0),
        "judged": judged,
        "n_total": len(items),
        "n_tested_cumulative": int(counters.get("n_tested_cumulative") or 0),
        "last_issue": counters.get("last_issue"),
        "min_batch_size": getattr(hypothesis, "MIN_BATCH_SIZE", None),
        "max_batch_size": getattr(hypothesis, "MAX_BATCH_SIZE", None),
        # MU-0：健康度。**这是"验证环节有没有在真的工作"的唯一可观测指标**。
        #   never_registered=True 表示注册簿里没有任何在途实验 —— 若出现，
        #   说明「前置注册·写入」的调度接线被误删（曾导致 usable 恒 0 数月无人察觉）。
        "health": _registry_health_for_status(),
        "items": items,
        "snapshot": {
            "available": bool(snapshot),
            "base_issue": (snapshot or {}).get("base_issue"),
            "generated_at": (snapshot or {}).get("generated_at"),
            "stale": bool((snapshot or {}).get("stale")),
            "counts": (snapshot or {}).get("counts"),
            "rules": snap_rules,
        },
        "note_zh": ("前置注册把「我找了 N 次」变成「我只检验了事先承诺的 M 条」："
                    "只有 passed（累计口径 Bonferroni 校正后 p<α）才允许影响出号；"
                    "provisional（批内显著但累计不显著）与 failed 一律不得使用。"),
    })


@router.get("/api/ml/diagnosis")
def ml_diagnosis(max_issues: int = Query(800, ge=100, le=3500),
                 max_numbers: int = Query(3, ge=1, le=12),
                 n_null: int = Query(20, ge=5, le=200),
                 colors: Optional[str] = Query(None),
                 force: bool = Query(False)):
    """V10-10「🤖 ML 诊断」卡：ML 通道是否可信（置换重要性 + 联合打乱标签零分布）。

    数据源唯一：``ml_model.diagnose_ml_channels()``（V10-08）。
    默认参数刻意比底层默认**更轻**（2 个通道 × 3 个号码 × 20 次零复制，约 10~30s），
    因为这是页面卡片的同步请求；``force=False`` 且 10 分钟内已有结果时直接返回缓存。

    **诚实边界**：真实彩票数据上 ``trusted=False`` 是**预期结论**（零假设为真，
    没有可学习的信号），不是系统故障 —— 返回体里 ``honesty_zh`` 字段必须原样展示。
    """
    import time as _time
    from . import ml_model

    colors_t = tuple(c.strip() for c in (colors or "red,blue").split(",") if c.strip())
    colors_t = tuple(c for c in colors_t if c in ("red", "blue")) or ("red", "blue")
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    # 缓存键必须含**数据集指纹**：只按参数缓存的话，开奖数据更新后 10 分钟内
    # 仍会返回上一批数据算出的 trustworthy 结论（缓存把结论"钉死"在旧样本上）。
    dataset_fp = f"{len(draws)}:{db.max_issue()}"
    cache_key = [list(colors_t), int(max_issues), int(max_numbers), int(n_null), dataset_fp]
    now = _time.time()
    # ---- D-3：缓存落盘持久化 + 后台刷新 ----
    # 实测一次诊断：默认参数(3号码/800期/B=20) 39.0s，standard(B=79) 56.1s。
    # 每次零复制都要重训 VotingClassifier(RF+HistGB)，是纯 CPU 重活；
    # 进程内 dict 缓存重启即失效 → 重启后第一次打开页面要空等几十秒，
    # 且同步 HTTP 很可能先被网关超时掐断。改为：
    #   ① 缓存写盘（重启后仍可用）；② 命中未过期 → 直接返回；
    #   ③ 过期 → 先返回旧结论（标 stale）+ 后台线程刷新；
    #   ④ 从未算过 → 立即返回 202 + task_id，前端轮询 /api/tasks/{id}。
    cached = _load_ml_diag_cache()
    if (not force and cached and list(cached.get("key") or []) == cache_key):
        age = now - float(cached.get("at") or 0)
        if age < _ML_DIAG_TTL:
            out = dict(cached["value"])
            out["cached"] = True
            out["cache_age_s"] = round(age, 1)
            return _jsonable(out)
        if not _ML_DIAG_JOB.get("running"):
            _kick_ml_diag_bg(draws, colors_t, int(max_issues), int(max_numbers),
                             int(n_null), cache_key)
        out = dict(cached["value"])
        out["cached"] = True
        out["stale"] = True
        out["cache_age_s"] = round(age, 1)
        out["refreshing"] = bool(_ML_DIAG_JOB.get("running"))
        return _jsonable(out)
    # 轻量参数（实测 ~7s）仍同步返回，让单测/CLI/小规模页面无需改造成轮询
    light = (int(n_null) <= 20 and int(max_numbers) <= 2 and int(max_issues) <= 400)
    if light and not force:
        try:
            diag = ml_model.diagnose_ml_channels(
                draws, colors=colors_t, max_issues=int(max_issues),
                max_numbers=int(max_numbers), n_null=int(n_null))
        except Exception as exc:                      # noqa: BLE001 - 诊断失败不 500
            return JSONResponse({"ok": False, "error": f"ML 诊断失败: {exc}"},
                                status_code=200)
        payload = _ml_diag_payload(diag, draws, cache_key, colors_t,
                                   max_issues, max_numbers, n_null)
        _save_ml_diag_cache({"key": cache_key, "at": now, "value": payload})
        return _jsonable(payload)
    # 从未算过 / force：交给后台任务，立即返回
    if force or not _ML_DIAG_JOB.get("running"):
        tid = _kick_ml_diag_bg(draws, colors_t, int(max_issues), int(max_numbers),
                               int(n_null), cache_key)
        if tid:
            return JSONResponse({"ok": True, "pending": True, "task_id": tid,
                                 "note_zh": "诊断在后台计算（单次约 40~90 秒），"
                                            "请轮询 /api/tasks/{task_id} 获取结果"},
                                status_code=202, background=None)


# ML 诊断结果缓存（进程内，TTL 见下）。诊断一次 10~30s，页面刷新不该反复重算。
_ML_DIAG_TTL = 600.0
_ML_DIAG_CACHE: Dict[str, Any] = {}
# D-3：后台诊断任务的进程内状态（防重复触发）
_ML_DIAG_JOB: Dict[str, Any] = {"running": False, "task_id": None}


def _ml_diag_cache_path():
    """诊断缓存的落盘位置（与其它 runtime 状态同目录）。"""
    return config.DATA_DIR / "ml_diag_cache.json"


def _load_ml_diag_cache() -> Optional[Dict]:
    """读持久化缓存；进程内已有则优先用进程内的（更新更近）。

    为什么落盘（D-3）：一次诊断 40~90 秒，进程内 dict 重启即失效，
    会让"每次重启后第一次打开 ML 诊断卡"都要空等几十秒。
    """
    mem = _ML_DIAG_CACHE.get("result")
    if mem:
        return mem
    try:
        path = _ml_diag_cache_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "key" in data:
            _ML_DIAG_CACHE["result"] = data
            return data
    except (ValueError, OSError):
        return None
    return None


def _save_ml_diag_cache(entry: Dict) -> None:
    """原子写盘（tmp + replace），失败只告警不影响返回。"""
    try:
        _ML_DIAG_CACHE["result"] = entry
        path = _ml_diag_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except (ValueError, OSError) as exc:  # noqa: BLE001
        print(f"[api] ML 诊断缓存写盘失败（不影响本次结果）: {exc}")


def _kick_ml_diag_bg(draws, colors_t, max_issues, max_numbers, n_null,
                     cache_key) -> Optional[str]:
    """把一次 ML 诊断丢进后台线程并登记 db 任务（D-3）。

    返回 task_id；已有任务在跑时返回 None（避免重复占用 CPU）。
    """
    if _ML_DIAG_JOB.get("running"):
        return None
    from . import ml_model

    def _work(progress):
        return ml_model.diagnose_ml_channels(
            draws, colors=colors_t, max_issues=int(max_issues),
            max_numbers=int(max_numbers), n_null=int(n_null))

    def _on_done(result):
        _ML_DIAG_JOB["running"] = False
        _ML_DIAG_JOB["task_id"] = None
        if isinstance(result, dict) and result.get("ok", True):
            payload = _ml_diag_payload(result, draws, cache_key, colors_t,
                                       max_issues, max_numbers, n_null)
            _save_ml_diag_cache({"key": cache_key, "at": time.time(), "value": payload})

    tid = _bg_run("ml_diagnosis", _work, on_done=_on_done)
    _ML_DIAG_JOB["running"] = True
    _ML_DIAG_JOB["task_id"] = tid
    return tid


def _ml_diag_payload(diag: Dict, draws, cache_key, colors_t,
                     max_issues, max_numbers, n_null) -> Dict:
    """把 diagnose_ml_channels 的结果整理成对外 payload（唯一构造点）。"""
    return {
        "ok": bool(diag.get("ok", True)),
        "trusted": bool(diag.get("trusted")),
        "tier": diag.get("tier"),
        "channel_tiers": diag.get("channel_tiers") or {},
        "channel_trusted": diag.get("channel_trusted") or {},
        "n_channels": diag.get("n_channels"),
        "n_channels_trusted": diag.get("n_channels_trusted"),
        "consistency_zh": diag.get("consistency_zh") or "",
        "advisory_only": bool(diag.get("advisory_only")),
        "advisory_warning_zh": diag.get("advisory_warning_zh") or "",
        "channels": diag.get("channels") or {},
        "params": {"max_issues": int(max_issues), "max_numbers": int(max_numbers),
                   "n_null": int(n_null), "colors": list(colors_t)},
        "n_draws": len(draws),
        "dataset_fingerprint": f"{len(draws)}:{db.max_issue()}",
        "cached": False,
        "honesty_zh": ("ML 通道的可信度用**真实开奖数据**上的置换重要性检验判定。"
                       "在纯随机数据（双色球即是）上 trusted=False 是**诚实且预期**的"
                       "结论：它说明模型没有学到可泛化的信号，不是故障、不是 bug，"
                       "也不代表模型代码有错。样本不足时「不显著」同样不能当作"
                       "「信号不存在」的证据。"),
        "note_zh": ("诊断只在少量号码上做以控制耗时；提高功效需扩大 max_numbers / "
                    "max_issues 与 n_null（standard 档 B>=79），代价是分钟级耗时。"),
    }
