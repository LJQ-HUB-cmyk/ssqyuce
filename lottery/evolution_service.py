"""自进化服务（P2 从 api_app 中提取，调度器与 API 共用）。

## 为什么独立成模块

`_evolution_step()` 被两处使用：
* 调度器每期开奖后自动跑（`_scheduler_step`）；
* `/api/evolution/run` 手动触发。

留在 api_app 会让 `routes_rules.py` 反向 import 应用模块（切分等于白做）。

## 它补上的关键缺口

`evolve_once` 只做「挖掘 + 注册 + 应用」，假说会**永远停在 `proposed`**
（实测 9 条全是 proposed）—— 因为没有任何环节把它们送去做 A/B 验证。

本函数补上这一步：对每条 proposed 假说跑配对回放，交给
`hypothesis.resolve_registry` 裁决，假说才可能晋升 `validated`（进而影响出号）
或被 `rejected`（退场）。**没有它，整个「验证规律」环节是空转的。**
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from . import config, db


def _evolution_step(limit: int = 0, use_llm: bool = True,
                    force: bool = False) -> dict:
    """每期开奖后自动跑的一轮自进化：挖掘 → 注册 → A/B 实战验证 → 裁决。

    为什么需要它：``evolve_once`` 只做「挖掘+注册+应用」，假说会永远停在
    ``proposed``（实测 9 条全是 proposed），因为**没有任何环节把它们送去做 A/B 验证**。
    本函数补上这一步——对每条 proposed 假说跑配对回放，把结果交给
    ``hypothesis.resolve_registry`` 裁决，假说才可能晋升 ``validated``（进而影响出号）
    或被 ``rejected``（退场）。

    成本控制：
    - ``limit`` 限制参与验证的历史期数（0=全部）；A/B 内部另有 600 期上限
    - ``use_llm`` 控制是否调用 LLM 提炼新假说（单次 1~2 分钟）
    - **幂等（2026-10-19 CPU 治理）**：同一期数据只自动进化一次。见下方说明。
    - 整体失败只记录不抛出，绝不影响当期预测生成

    ⚠️ 为什么要幂等（实测根因）
    -------------------------
    ``_scheduler_step`` 由 ``_scheduler_loop`` 每 1800s 调用一次；在**开奖日**
    （周二/四/日）过了 21:35 之后，每轮都会走到这里。而这一天剩下的时间里数据
    并不会再变（``inserted_new: 0``），于是**同一份数据被反复跑完整进化**：
    日志实测 2026-10-18（周日）14:13 / 15:06 / 15:56 三轮，每轮 19~22 分钟。
    这就是「容器 CPU 长期 100%」的第二层成因（第一层是 ``_rebuild_probs`` 的
    冗余模型构建，已单独修复）。

    修法：把「已进化到的数据指纹（期数 + 末期号）」记在 ``data/evolution_state.json``；
    指纹没变就**跳过**（返回 ``skipped=True``）。数据一更新指纹就变 ⇒ 立刻恢复进化。
    手动触发（``/api/evolution/run``）传 ``force=True``，永不被跳过。

    返回本轮摘要，便于调度日志与 ``/api/evolution/status`` 观测。
    """
    from . import battle, evolution, hypothesis

    draws = db.load_draws()
    if not draws:
        return {"ok": False, "reason": "本地暂无开奖数据"}

    # ---- 幂等闸门：同一份数据只自动进化一次 ----
    fingerprint = f"{len(draws)}:{draws[-1].get('issue')}"
    if not force:
        state = _evolution_state_load()
        if state.get("last_fingerprint") == fingerprint:
            return {"ok": True, "skipped": True,
                    "skipped_reason": "该期数据已完成进化（幂等跳过）",
                    "fingerprint": fingerprint,
                    "last_run_at": state.get("last_run_at"),
                    "note_zh": ("本期数据未变化，进化已跑过 → 跳过（避免每 30 分钟"
                                "重复跑完整 A/B 验证；手动触发请用 /api/evolution/run）")}

    if limit:
        draws = draws[-int(limit):]

    summary: dict = {"ok": True}

    # 1) 挖掘 + 注册新假说（LLM 或纯统计）
    mined = hypothesis.mine_hypotheses({}, draws, use_llm=use_llm)
    reg = hypothesis.register_hypotheses(mined.get("hypotheses") or [])
    summary["mined"] = {"stat": mined.get("stat_count"), "llm": mined.get("llm_count"),
                        "llm_used": mined.get("llm_used"), "llm_error": mined.get("llm_error")}
    summary["registered"] = reg

    # 1b) V6：把「规律工坊」里已验证过阶段的 LLM 规律也注册进假说库
    #     （只收 support/reject/regime_only，unmappable/noise 一律不入库）
    try:
        from . import llm_lab
        if llm_lab.load_claims().get("claims"):
            summary["llm_lab_registered"] = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        summary["llm_lab_registered"] = {"error": f"{type(e).__name__}: {e}"}

    # 2) 对**全部** proposed 假说做实战验证 —— 这是它们唯一的晋升通道。
    #    V6：按假说类型分派验证口径。维度化的走 as-of 筛选条件回放（任何假说都有
    #    非平凡数字），其余走结构/方法权重的 A/B 配对回放。
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    proposed = [h for h in hyps if h.get("status") == "proposed"]
    results = []
    for h in proposed:
        act = h.get("action") if isinstance(h.get("action"), dict) else {}
        try:
            if act.get("type") in ("prefer_red_filter", "prefer_blue_filter"):
                results.append(evolution.verify_discovery_hypothesis(h, draws, start=200))
            else:
                results.append(evolution.verify_hypothesis_ab(
                    h, draws, n_tickets=5, start=max(300, len(draws) - 600)))
        except Exception as e:  # noqa: BLE001
            print(f"[evolution] 假说 {h.get('id')} 验证失败: {e}")
    summary["verified"] = len(results)

    # 3) 裁决：validated / rejected / 样本不足
    resolution = hypothesis.resolve_registry(results) if results else {}
    summary["resolution"] = {
        "validated": resolution.get("validated"),
        "rejected": resolution.get("rejected"),
        "insufficient": len(resolution.get("insufficient") or []),
        "unchanged": resolution.get("unchanged"),
    }

    # 3b) V6：阶段时效标注（每条假说带「现在是否还有效」）
    try:
        summary["regime_annotated"] = evolution.annotate_hypotheses(draws)
    except Exception as e:  # noqa: BLE001
        summary["regime_annotated"] = {"error": f"{type(e).__name__}: {e}"}

    # 4) 报告当前生效的出号策略覆盖项
    st = evolution.evolution_status()
    summary["by_status"] = st.get("by_status")
    summary["active_overrides"] = st.get("active_overrides")

    # 5) V10-11：决策技能达标性判定（Wilson 下界 ≤ 0.5 → 自动关停决策通道）。
    #    写 data/llm_config.json（不写 os.environ）；无决定性样本时**不**写。
    #    失败只降级、不影响预测（沿用本函数的既有约定）。
    try:
        summary["decision_skill"] = _maybe_auto_disable_decision()
    except Exception as e:  # noqa: BLE001
        summary["decision_skill"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print("[evolution] 本轮完成:", summary["resolution"], "| 库:", summary["by_status"])
    # 记录本次进化对应的数据指纹（幂等闸门的数据源）。写失败只记录、不影响结果。
    try:
        _evolution_state_store(fingerprint, summary)
    except Exception as e:  # noqa: BLE001
        print("[evolution] 状态记录失败（不影响进化结果）:", e)
    summary["fingerprint"] = fingerprint
    return summary


def _evolution_state_path():
    """进化幂等状态文件路径（与 ``adaptive_state.json`` 同目录，属运行时状态）。"""
    return config.DATA_DIR / "evolution_state.json"


def _evolution_state_load() -> dict:
    """读幂等状态；文件缺失/损坏一律返回空 dict（当作「从未进化」）。"""
    import json as _json
    try:
        p = _evolution_state_path()
        if not p.exists():
            return {}
        raw = _json.loads(p.read_text(encoding="utf-8") or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _evolution_state_store(fingerprint: str, summary: dict) -> None:
    """记录「已进化到的数据指纹」（原子替换）。"""
    import json as _json
    import os as _os
    import time as _time
    p = _evolution_state_path()
    payload = {
        "last_fingerprint": str(fingerprint),
        "last_run_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_resolution": summary.get("resolution"),
        "schema": 1,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _os.replace(tmp, p)
