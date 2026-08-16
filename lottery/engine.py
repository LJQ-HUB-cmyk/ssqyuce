"""集成预测引擎：统计模型 + LLM 通道 → 硬约束过滤 → 置信度评分 → Top-N 输出。"""
from __future__ import annotations

import json
import random
from typing import Dict, List, Optional

import numpy as np

from . import backtest as BT
from . import config, db, features as F, llm_client, models as M

R_MAX, B_MAX = 33, 16


# ---------- 硬约束 ----------

def _last_sums(draws: List[Dict], n: int = 500) -> np.ndarray:
    return F.sums(draws[-n:]) if len(draws) >= n else F.sums(draws)


def constraint_ctx(draws: List[Dict]) -> Dict:
    s = _last_sums(draws)
    zc = F.zone_counts(draws[-500:] if len(draws) >= 500 else draws)
    zc_hist = {}
    for z in zc:
        zc_hist[z] = zc_hist.get(z, 0) + 1
    top_zones = [k for k, _ in sorted(zc_hist.items(), key=lambda x: -x[1])[:10]]
    oc = F.odd_counts(draws[-500:] if len(draws) >= 500 else draws)
    odd_hist = {}
    for o in oc:
        odd_hist[int(o)] = odd_hist.get(int(o), 0) + 1
    odd_range = sorted(
        (k for k, v in odd_hist.items() if v / max(1, len(oc)) >= 0.02)
    )
    return {
        "sum_min": float(np.percentile(s, 5)),
        "sum_max": float(np.percentile(s, 95)),
        "top_zones": top_zones,
        "odd_range": odd_range if odd_range else [2, 3, 4],
    }


def pass_constraints(reds: List[int], blue: int, ctx: Dict) -> bool:
    s = sum(reds)
    if not (ctx["sum_min"] <= s <= ctx["sum_max"]):
        return False
    z1 = sum(1 for r in reds if 1 <= r <= 11)
    z2 = sum(1 for r in reds if 12 <= r <= 22)
    z3 = 6 - z1 - z2
    if (z1, z2, z3) not in ctx["top_zones"]:
        return False
    odd = sum(1 for r in reds if r % 2 == 1)
    if odd not in ctx["odd_range"]:
        return False
    return True


# ---------- 统计采样 ----------

def _weighted_sample(weights: np.ndarray, k: int, rng: random.Random) -> List[int]:
    pool = list(range(1, len(weights) + 1))
    w = weights.astype(float).copy()
    chosen = []
    for _ in range(k):
        if w.sum() <= 0:
            w = np.ones_like(w)
        p = (w / w.sum()).tolist()
        pick = rng.choices(pool, weights=p, k=1)[0]
        chosen.append(pick)
        w[pick - 1] = 0.0
    return sorted(chosen)


def sample_stat_ticket(red_blend: np.ndarray, blue_blend: np.ndarray,
                       ctx: Dict, rng: random.Random, uniform: bool = False) -> Optional[Dict]:
    """按混合概率加权不放回抽样，硬约束不过则重试。"""
    for _ in range(60):
        if uniform:
            reds = sorted(rng.sample(range(1, R_MAX + 1), 6))
            blue = rng.randint(1, B_MAX)
        else:
            reds = _weighted_sample(red_blend, 6, rng)
            blue = _weighted_sample(blue_blend, 1, rng)[0]
        if pass_constraints(reds, blue, ctx):
            return {"reds": reds, "blue": blue}
    return None


def ensemble_mass(red_blend: np.ndarray, blue_blend: np.ndarray,
                  reds: List[int], blue: int) -> float:
    """候选号在混合分布上的概率质量（0-100 比例尺）。"""
    r_mass = float(np.mean(red_blend[np.array(reds) - 1]))
    b_mass = float(blue_blend[blue - 1])
    norm_r = float(np.max(red_blend))
    norm_b = float(np.max(blue_blend))
    if norm_r <= 0:
        norm_r = 1.0
    if norm_b <= 0:
        norm_b = 1.0
    return 100.0 * (0.75 * r_mass / norm_r + 0.25 * b_mass / norm_b)


def _brier_blend(models_dict: Dict, draws: List[Dict]) -> tuple:
    """基于 Brier score 的模型概率融合（替代等权平均）。"""
    n_eval = min(50, len(draws) - 300)
    if n_eval < 10:
        # 数据不足，等权
        n = len(models_dict)
        red_blend = sum(m["red"] for m in models_dict.values()) / n
        blue_blend = sum(m["blue"] for m in models_dict.values()) / n
        return red_blend, blue_blend
    
    weights = {}
    for name, model in models_dict.items():
        red_p = model["red"]
        blue_p = model["blue"]
        brier_red, brier_blue, count = 0.0, 0.0, 0
        for i in range(-n_eval, 0):
            t = draws[i]
            tr = np.zeros(33)
            for r in t["reds"]: tr[r-1] = 1.0
            brier_red += float(np.mean((red_p - tr) ** 2))
            tb = np.zeros(16)
            tb[t["blue"]-1] = 1.0
            brier_blue += float(np.mean((blue_p - tb) ** 2))
            count += 1
        if count > 0:
            avg = (brier_red + brier_blue) / (2 * count)
            weights[name] = max(0.01, 1.0 / max(avg, 0.001))
        else:
            weights[name] = 0.01
    total = sum(weights.values())
    weights = {k: v/total for k, v in weights.items()}
    print(f"[engine] Brier 权重: {weights}")
    
    red_blend = np.zeros(33)
    blue_blend = np.zeros(16)
    for name, model in models_dict.items():
        w = weights.get(name, 0)
        red_blend += w * model["red"]
        blue_blend += w * model["blue"]
    red_blend = red_blend / red_blend.sum() * 6
    blue_blend = blue_blend / blue_blend.sum()
    return red_blend, blue_blend


# ---------- 主流程 ----------

def build_context(draws: List[Dict], stats: Dict, patterns: List[Dict]) -> Dict:
    return {
        "stats": stats,
        "recent": stats.get("recent", []),
        "patterns": [
            {k: p.get(k) for k in ("key", "name_zh", "kind", "grade", "margin", "p_value")}
            for p in patterns if p.get("grade") in ("A", "B")
        ],
        "constraints": constraint_ctx(draws),
    }


def llm_tickets(draws: List[Dict], stats: Dict, patterns: List[Dict],
                rng: random.Random) -> List[Dict]:
    """多模型 LLM 采样生成候选（失败自动降级为空）。"""
    from concurrent.futures import ThreadPoolExecutor
    if config.LLM_DISABLED:
        return []
    model_cfgs = config.llm_model_list()
    if not model_cfgs:
        print("[llm] 无可用模型配置，跳过 LLM 通道")
        return []
    ctx = build_context(draws, stats, patterns)

    # 观察轮次
    obs = llm_client.chat_json(
        llm_client.SYSTEM_BASE,
        llm_client.observations_prompt(
            llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"]),
        max_tokens=1600, temperature=0.7, model_cfg=model_cfgs[0],
    )
    if obs is None:
        print("[llm] 观察生成失败，跳过 LLM 通道")
        return []

    n_models = len(model_cfgs)
    tickets: List[Dict] = []

    def _ticket_call(cfg: Dict) -> List[Dict]:
        res = llm_client.chat_json(
            llm_client.SYSTEM_BASE,
            llm_client.tickets_prompt(
                llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"], obs),
            max_tokens=2000, temperature=0.9, model_cfg=cfg,
        )
        out: List[Dict] = []
        if not res or not isinstance(res.get("tickets"), list):
            return out
        for t in res["tickets"]:
            try:
                reds = sorted(int(x) for x in t["reds"])
                blue = int(t["blue"])
                conf = float(t.get("confidence", 50))
                if len(set(reds)) != 6 or not all(1 <= x <= R_MAX for x in reds):
                    continue
                if not 1 <= blue <= B_MAX:
                    continue
                out.append({
                    "reds": reds, "blue": blue, "method": f"llm:{cfg['name']}",
                    "confidence": conf,
                    "reasoning": str(t.get("reasoning", ""))[:300],
                    "patterns_used": [str(x) for x in t.get("patterns_used", [])],
                })
            except (TypeError, ValueError):
                continue
        return out

    calls = [model_cfgs[i % n_models] for i in range(config.LLM_SAMPLES)]
    with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as ex:
        futures = [ex.submit(_ticket_call, cfg) for cfg in calls]
        for f in futures:
            tickets.extend(f.result())
    return tickets


def predict_next(draws: List[Dict], use_llm: Optional[bool] = None,
                 n_tickets: Optional[int] = None, persist: bool = True) -> Dict:
    """对下一期生成预测。"""
    if use_llm is None:
        use_llm = not config.LLM_DISABLED
    n_tickets = n_tickets or config.N_TICKETS
    issue = BT.next_issue(draws[-1]["issue"])

    stats = F.compute_features(draws)
    patterns = db.load_patterns()
    ctx = constraint_ctx(draws)

    # 统计模型混合概率（Brier 加权融合）
    bl = M.build_models(draws)
    red_blend, blue_blend = _brier_blend(bl, draws)

    rng = random.Random()
    candidates: List[Dict] = []

    # 各统计模型分别采样
    for name, model in bl.items():
        for _ in range(2):
            t = sample_stat_ticket(model["red"], model["blue"], ctx, rng)
            if t:
                t["method"] = f"stat:{name}"
                candidates.append(t)
    # 均匀对照
    for _ in range(2):
        t = sample_stat_ticket(None, None, ctx, rng, uniform=True)
        if t:
            t["method"] = "uniform"
            candidates.append(t)

    # LLM 候选
    llm_cands = llm_tickets(draws, stats, patterns, rng) if use_llm else []
    candidates.extend(llm_cands)
    llm_models_used = sorted({
        t["method"].split(":", 1)[1] for t in llm_cands if t["method"].startswith("llm:")
    })

    # 去重 + 评分
    seen = set()
    scored: List[Dict] = []
    for t in candidates:
        key = (tuple(t["reds"]), t["blue"])
        if key in seen:
            continue
        seen.add(key)
        if t["method"].startswith("llm:"):
            em = ensemble_mass(red_blend, blue_blend, t["reds"], t["blue"])
            conf = round(20 + 0.45 * (0.5 * t.get("confidence", 50) + 0.5 * em), 1)
        else:
            em = ensemble_mass(red_blend, blue_blend, t["reds"], t["blue"])
            conf = round(10 + 0.25 * em, 1)
        t["confidence"] = min(100.0, max(1.0, conf))
        scored.append(t)

    # 蓝球分散
    scored.sort(key=lambda x: -x["confidence"])
    picked: List[Dict] = []
    blue_count = {}
    for t in scored:
        if len(picked) >= n_tickets:
            break
        if blue_count.get(t["blue"], 0) >= max(2, n_tickets // 5):
            continue
        blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
        picked.append(t)

    result = {
        "issue": issue,
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "target_draw": {"issue": draws[-1]["issue"], "date": draws[-1]["date"],
                        "reds": draws[-1]["reds"], "blue": draws[-1]["blue"]},
        "tickets": picked,
        "llm_used": use_llm and bool(llm_cands),
        "llm_models": llm_models_used,
        "red_probs": red_blend.tolist(),
        "blue_probs": blue_blend.tolist(),
        "patterns_summary": {
            "A": sum(1 for p in patterns if p["grade"] == "A"),
            "B": sum(1 for p in patterns if p["grade"] == "B"),
            "C": sum(1 for p in patterns if p["grade"] == "C"),
        },
        "note": ("样本外回测未发现稳定显著的规律，预测仅基于统计结构的均衡建议；"
                 "置信度为模型结构分，不构成中奖概率。理性购彩。"),
    }
    if persist:
        db.save_features(issue, stats)
        db.save_predictions(issue, picked)
    return result
