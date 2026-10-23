"""前向回测判据（V20 / L0）：任何「有信号」的结论都必须再过一个确定性检验。

## 为什么需要这个模块（V19 §2.3 实测）

现有的「通道是否可信」判据只有**置换检验**（`ml_model.diagnose_ml_channel`）：
把标签打乱重训 N 次，看真实重要性是否高于零分布。它在数学上是对的，
但**不够** —— 2026-10-23 实测：

* `ml_blue` 在 standard 档（B=200）下 `p=0.010`、`trusted=True`，换两个种子
  （0 / 7）分别是 `p=0.010` / `p=0.005`，**结论稳定**；
* 同一份数据做**严格前向回测**（频率法，600 期）：
  命中 `30/600 = 5.00%`，而随机基线是 `1/16 = 6.25%`，**z = -1.26**。

**置换检验显著 ≠ 可预测。** 原因：置换检验问的是「模型有没有学到超出噪声的结构」，
它可以在**只有 600 期、测试集 120 行**的样本上因轻微偏离而显著，
而这点偏离在真实前向预测里毫无价值（甚至为负）。

因此本模块提供第二道、也是更朴素的一道关：**能不能在时间上向前预测**。

## 判据设计（双重门槛）

一个通道/方法要被称为 `signal_found`，必须**同时**满足：

1. **置换检验** `p < alpha`（由各诊断模块自己给出）；
2. **前向回测** z > `WF_Z_MIN`（默认 1.0），即命中率高于随机基线一个标准差以上。

任一条不过 → 只能是 `monitor`。

## 为什么不直接用 z 的显著性（如 z > 1.96）

因为我们的目的不是「证明有优势」，而是「**排除明显没有优势**」。
在 n=600 期上，z=1.96 要求相对提升约 +8%（蓝球），这个门槛在彩票上
是不现实的高（真实最强效应约 +5%，见 P7 §6.1）。用 z>1.0 作为
**最低诚信线**更合适：它只要求「不比随机差，且至少有一点点正向迹象」，
同时把「明显差于随机」（如 z=-1.26）明确判死。

这个门槛是**宽松的**——宽松是有意的：宁可让一个弱信号进入 monitor 观察，
也不要因为门槛太高而给出「无法判断」这种没用的结论。

## 与既有模块的关系

* 只**读** `battle.replay` 的结果，不重复实现 walk-forward（那套 as-of 纪律
  已经过 3 年迭代与大量测试，重写只会引入新 bug）；
* 不修改任何既有判据的 α（`ALPHA`/`ERA_FAMILY_M` 等全部不动）。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from . import battle

__all__ = [
    "WF_Z_MIN",
    "walk_forward_check",
    "combine_gates",
    "WF_METHODS",
]

#: 前向回测的最低 z 门槛（诚信线，不是显著性线）。
#:
#: 取值理由见模块 docstring 末段：只要求「不比随机差 + 有一点点正向迹象」。
#: 调高它会得到大量「无法判断」，调低它会把纯噪声当信号 —— 1.0 是两者之间
#: 唯一有明确语义的位置（= 命中率高于基线的 t 统计量）。
WF_Z_MIN = 1.0

#: 稳健性检查用的种子集合（V20 修正）。
#:
#: ## 为什么必须多种子（2026-10-23 实测踩到）
#:
#: 单一种子的前向回测可以被抽样噪声愚弄。`stat:freq` 红球通道实测：
#:
#:     seed=42,  start=2917  z=+1.74  → 通过
#:     seed=7,   start=2917  z=+0.83  → 不通过
#:     seed=99,  start=2917  z=-0.33  → 不通过
#:
#: **同一方法、同一数据，换个种子就从"通过"变"不通过"** —— 这说明那个 z=1.74
#: 是出号随机性（`battle` 每期用 `random.Random(seed+t)` 采样票单）造成的噪声，
#: 不是方法本身的优势。若不检查，判据会把噪声盖章成"有信号"。
#:
#: 因此要求：**多种子的 z 中位数 > 门槛**，且**不能有过半数的种子显著为负**。
WF_SEEDS: List[int] = [42, 7, 99]

#: 可用于前向回测的方法（`battle.replay` 的可回放集合）。
#: `ml` / `llm` 不在其中 —— 它们不可回放（见 `battle.NON_REPLAYABLE_METHODS`），
#: 所以对它们只能报告「无前向回测证据」，而不能伪造一个数字。
WF_METHODS: List[str] = list(battle.DEFAULT_METHODS)


def _binomial_z(hits: float, n: float, p0: float) -> Optional[float]:
    """命中数相对二项基线的 z。n<=0 或无方差时返回 None。"""
    if n is None or n <= 0:
        return None
    p0 = float(p0)
    if not (0.0 < p0 < 1.0):
        return None
    sd = math.sqrt(n * p0 * (1.0 - p0))
    if sd <= 0:
        return None
    return (float(hits) - n * p0) / sd


def walk_forward_check(draws: Sequence[Dict], method: str = "stat:freq",
                       *, n_tickets: int = 5, start: int = 300,
                       seed: int = 42, color: str = "blue") -> Dict:
    """对某个可回放方法做前向回测，并给出相对随机基线的 z 值。

    ``color`` 决定评估哪个通道：
      * ``"blue"``：蓝球命中率 vs 基线 1/16（**默认**。蓝球注数 = 票数，
        每注只押 1 个蓝球，基线干净、样本效率最高）；
      * ``"red"``：平均红球命中 vs 基线 6*6/33（每注 6 个红球，
        命中数较多但方差也大）。

    返回 ``{ok, method, color, replayable, n, hit_rate, baseline, z,
    passed, reason_zh, ...}``；``passed`` 只在 ``z > WF_Z_MIN`` 时为 True。

    **不做任何统计修正**：这只是道门槛，不产出 p 值、不做多重比较——
    多重比较由上层（各诊断模块自己的置换检验）负责。
    """
    out: Dict = {
        "ok": False, "method": method, "color": color, "replayable": None,
        "n": 0, "hits": 0.0, "hit_rate": None, "baseline": None, "z": None,
        "z_min": WF_Z_MIN, "passed": False, "reason_zh": "",
    }
    try:
        rep = battle.replay_method(method, list(draws or []), n_tickets=int(n_tickets),
                            start=int(start), seed=int(seed))
    except Exception as e:                     # noqa: BLE001
        out["reason_zh"] = f"前向回测执行失败（{type(e).__name__}: {e}）"
        return out

    if not rep.get("replayable", True):
        out["replayable"] = False
        out["reason_zh"] = (
            f"方法 {method!r} 不可历史回放（{rep.get('reason') or '原因未给出'}）："
            "无法提供前向回测证据。这不等于「无效」，而是「无法验证」——"
            "按诚信原则不得据此声称有信号。")
        return out

    per = rep.get("per_issue") or []
    # 口径（易错，V20 修正过一次）：
    #   ``blue_hit`` 是**该期 5 注里命中的注数**（0..n_tickets），不是 0/1 布尔。
    #   第一版按「blue_hit>0 的期数」统计，得到 601 期命中 0 次的荒谬结论
    #   （z=-6.33）—— 实际同期 blue_hit_rate 是 0.0622，接近基线。
    #   正确做法：命中数按注累计，分母是**总注数**。
    if color == "red":
        hits = float(sum(float(r.get("red_hits") or 0.0) for r in per))
        n = float(len(per))
        # 每注 6 个红球，随机命中 6*6/33
        base = 6.0 * 6.0 / 33.0
    else:
        # ⚠️ 绝不能用 per_issue 的 ``blue_hit``：那是 ``round(mean(blue_hits))``，
        # 即「该期各注蓝球命中的平均值**取整**」——5 注中 1 注时 mean=0.2、round=0，
        # 于是全样本累计恒为 0（V20 实测踩到，得出 z=-14 的荒谬结论）。
        # 正确口径是 battle 自己的 ``blue_hit_rate`` = 逐注平均命中率；
        # 命中注数 = rate × 总注数。
        n = float(sum(int(r.get("n_tickets") or rep.get("n_tickets_per_issue") or 1)
                      for r in per))
        rate = rep.get("blue_hit_rate")
        hits = float(rate) * n if rate is not None else 0.0
        base = 1.0 / 16.0

    out["ok"] = True
    out["replayable"] = True
    out["n"] = int(n)
    out["hits"] = round(hits, 4)
    out["baseline"] = round(base, 6)
    if n > 0:
        out["hit_rate"] = round(hits / n, 6)

    if color == "red":
        # 红球用「逐期平均命中」的 t 统计量（每期一个平均值，方差由数据估计）
        vals = [float(r.get("red_hits") or 0.0) for r in per]
        if len(vals) >= 2:
            import numpy as _np
            arr = _np.asarray(vals, dtype=float)
            sd = float(arr.std(ddof=1))
            if sd > 0:
                out["z"] = round((float(arr.mean()) - base) / (sd / math.sqrt(len(arr))), 4)
    else:
        out["z"] = (round(_binomial_z(hits, n, base), 4)
                    if _binomial_z(hits, n, base) is not None else None)

    z = out["z"]
    if z is None:
        out["reason_zh"] = "样本不足（n<=1 或无方差），无法计算前向回测 z 值。"
        return out

    # ---- 多种子稳健性（V20 修正，见 WF_SEEDS 注释）----
    # 单次运行的 z 可能来自出号随机性；用固定种子集合各跑一遍，
    # 只有**中位数**过门槛才算通过。这一步把"换种子就翻转"的假信号挡在门外。
    _zs: List[float] = []
    for _sd in WF_SEEDS:
        try:
            _r2 = battle.replay_method(method, list(draws or []), n_tickets=int(n_tickets),
                                       start=int(start), seed=int(_sd))
            if not _r2.get("replayable", True):
                continue
            _per2 = _r2.get("per_issue") or []
            if color == "red":
                _vals = [float(x.get("red_hits") or 0.0) for x in _per2]
                if len(_vals) >= 2:
                    import numpy as _np
                    _arr = _np.asarray(_vals, dtype=float)
                    _sdv = float(_arr.std(ddof=1))
                    if _sdv > 0:
                        _zs.append((float(_arr.mean()) - base) / (_sdv / math.sqrt(len(_arr))))
            else:
                _nt = float(sum(int(x.get("n_tickets") or _r2.get("n_tickets_per_issue") or 1)
                                for x in _per2))
                _rt = _r2.get("blue_hit_rate")
                _hz = _binomial_z(float(_rt) * _nt, _nt, base) if _rt is not None else None
                if _hz is not None:
                    _zs.append(_hz)
        except Exception:                      # noqa: BLE001
            continue
    out["z_by_seed"] = [round(v, 4) for v in _zs]
    if _zs:
        _sorted = sorted(_zs)
        _mid = len(_sorted) // 2
        _median = (_sorted[_mid] if len(_sorted) % 2
                   else 0.5 * (_sorted[_mid - 1] + _sorted[_mid]))
        out["z_median"] = round(float(_median), 4)
        out["n_negative"] = int(sum(1 for v in _zs if v < 0))
        out["robust"] = bool(_median > WF_Z_MIN)
    else:
        out["z_median"] = None
        out["n_negative"] = 0
        out["robust"] = False

    # 红球口径是「每期平均命中个数」（不是比率），显示时不能套百分号 ——
    # 否则会出现「命中率 112.65%」这种荒谬读数（V20 实测踩到）。
    def _fmt(val, _base):
        return f"{val:.3f} 个/期" if color == "red" else f"{val:.2%}"

    if z > WF_Z_MIN and out.get("robust"):
        out["passed"] = True
        out["reason_zh"] = (
            f"前向回测通过：{_fmt(out['hit_rate'], base)} vs 随机基线 {_fmt(base, base)}，"
            f"z={z:.2f} > {WF_Z_MIN}，且多种子稳健（中位数 {out.get('z_median')}，"
            f"{int(n)} 期）。注意：这只是「不比随机差且有一点正向迹象」的**最低诚信线**，"
            "不是「已证明有优势」。")
    else:
        _extra = ""
        if z > WF_Z_MIN and not out.get("robust"):
            _extra = (f" ⚠️ 单次 z={z:.2f} 看似过线，但**多种子不稳健**："
                      f"z 中位数 {out.get('z_median')}（各次 {out.get('z_by_seed')}）——"
                      "这正是「换个种子就翻转」的噪声，不能当信号。")
        # 判据是「中位数 > 门槛」，不是「单次 z > 门槛」；文案必须说清是哪条没过，
        # 否则会出现「z=1.74 ≤ 1.0」这种自相矛盾的句子（V20 实测踩到）。
        _mid = out.get("z_median")
        if _mid is None:
            _head = f"前向回测未过：{_fmt(out['hit_rate'], base)} vs 随机基线 {_fmt(base, base)}（{int(n)} 期）。"
        elif float(_mid) > WF_Z_MIN:
            _head = (f"前向回测未过：多种子 z 中位数 {float(_mid):.2f} 虽 > {WF_Z_MIN}，"
                     f"但单次读数不稳（各次 {out.get('z_by_seed')}）。")
        else:
            _head = (f"前向回测未过：{_fmt(out['hit_rate'], base)} vs 随机基线 {_fmt(base, base)}，"
                     f"多种子 z 中位数 {float(_mid):.2f} ≤ {WF_Z_MIN}（{int(n)} 期）。")
        out["reason_zh"] = (_head
                            + ("表现**低于**随机。" if (z < 0 and (out.get("n_negative") or 0) > 0)
                               else "与随机无实质差别。")
                            + _extra)
    return out


def combine_gates(perm_p: Optional[float], wf: Optional[Dict],
                  *, alpha: float = 0.05, z_min: Optional[float] = None) -> Dict:
    """把「置换检验 p」与「前向回测 z」合并成一个判定（V20 双重门槛）。

    ## 判据

    ``signal_found`` 必须**同时**满足：

    1. ``perm_p < alpha``（置换检验显著）；
    2. ``wf.z > z_min``（前向回测高于基线一个诚信线以上）。

    任一条不满足 → ``monitor``。

    ## 为什么必须两条都要

    单看置换检验会把「只在 600 期样本上稍微偏离」的通道判成有信号
    （`ml_blue` 实测 p=0.010 但前向回测 z=-1.26）；
    单看前向回测则分辨力不足（没有零分布，无法区分「弱真实信号」与「样本大一点的噪声」）。
    两者结合：置换检验负责「是不是超出噪声」，前向回测负责「是不是真能向前用」。

    ## 缺数据时怎么判

    * ``perm_p`` 为 None（未做置换检验）→ 不能声称有信号，``monitor``；
    * ``wf`` 为 None 或 ``replayable=False``（不可回放）→ 同上，``monitor``；
      **不**因为「无法验证」就放行，也不因为「无法验证」就判死 —— 明确标 ``unverifiable``。

    返回 ``{tier, passed_perm, passed_wf, gates_failed, reason_zh, unverifiable}``。
    """
    z_min = WF_Z_MIN if z_min is None else float(z_min)
    passed_perm = (perm_p is not None and float(perm_p) < float(alpha))
    wf = wf or {}
    wf_replayable = wf.get("replayable")
    passed_wf = bool(wf.get("passed")) and (wf.get("z") is not None
                                            and float(wf.get("z")) > z_min)
    unverifiable = (wf_replayable is False) or (wf.get("z") is None)

    failed: List[str] = []
    if not passed_perm:
        failed.append("permutation")
    if not passed_wf:
        failed.append("walk_forward")

    if passed_perm and passed_wf:
        return {
            "tier": "signal_found", "passed_perm": True, "passed_wf": True,
            "gates_failed": [], "unverifiable": False,
            "reason_zh": (
                "双重门槛通过：置换检验显著，且前向回测高于随机基线。"
                "可继续研究；仍不承诺命中率提升（双色球为独立随机事件）。"),
        }
    if unverifiable and passed_perm:
        return {
            "tier": "monitor", "passed_perm": True, "passed_wf": False,
            "gates_failed": ["walk_forward"], "unverifiable": True,
            "reason_zh": (
                "置换检验显著，但**无法取得前向回测证据**（方法不可回放或样本不足）。"
                "按诚信原则降为 monitor：不可验证的证据不得当作信号。"),
        }
    detail = []
    if not passed_perm:
        detail.append(f"置换检验不显著（p={perm_p}）" if perm_p is not None
                      else "未做置换检验")
    if not passed_wf:
        _z = wf.get("z")
        detail.append(f"前向回测 z={_z} ≤ {z_min}" if _z is not None
                      else "前向回测无有效 z 值")
    return {
        "tier": "monitor", "passed_perm": passed_perm, "passed_wf": passed_wf,
        "gates_failed": failed, "unverifiable": unverifiable,
        "reason_zh": "双重门槛未过：" + "；".join(detail) + " → 只监控，不参与出号。",
    }
