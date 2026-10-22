"""V4 蓝球模式与覆盖规划（S6）。

## 迁移说明（D-02 拆分 S6）

内容从 `lottery/engine.py` 原 759–1026 行**逐字迁移**，逻辑未改一行。
迁出：`_min_significant_blue_sample` / `_blue_fallback_min_sample` / `_apply_blue_mode` /
`blue_coverage_plan` / `_plan_blue_coverage` / `_pick_blue_coverage` / `_pick_legacy_blue`。

## 诚实边界（原文保留）

蓝球覆盖优化的作用是**提高「至少中一次蓝球」的概率**（P 从 1-(15/16)^n 抬到 min(n,16)/16），
**但对期望收益一分钱都不改变** —— `E[命中注数]` 恒为 n/16，与是否去重无关。
返奖比因此恒为 5/32 = 15.625%，与买几注无关。这是组合数学结论，不是实现细节。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from . import config        # noqa: F401
from .engine_base import R_MAX, B_MAX, TICKET_COST  # noqa: F401  纯常量，无循环
from . import methods as METH  # noqa: F401  与拆前 engine.py 相同的别名




def _min_significant_blue_sample(base_rate: Optional[float] = None,
                                 target_rate: Optional[float] = None,
                                 z: float = 1.96) -> int:
    """反推「能把 base_rate 与 target_rate 区分开」所需的最小样本量。

    单期蓝球命中是伯努利变量（命中 1 / 未中 0），命中率估计的标准误为
    sd/sqrt(n)，其中 sd = sqrt(p*(1-p))。要让观测到的下跌不是噪声，样本量必须
    满足 z * sd / sqrt(n) <= |base_rate - target_rate|，即

        n >= (z * sd / delta)^2

    取 p = 1/16（sd ≈ 0.2421）、delta = 6.25% - 4.5% = 0.0175、z = 1.96：
    n >= (1.96 * 0.2421 / 0.0175)^2 ≈ 735 期。

    也就是说 30 期窗口（标准误 ≈ 4.42%）在数学上**根本不可能**判定
    「4.5% 与 6.25% 谁更好」——一个标准误就覆盖了整个 1.75% 的差额。
    """
    if base_rate is None:
        base_rate = getattr(config, "BLUE_RANDOM_RATE", 1.0 / B_MAX)
    if target_rate is None:
        target_rate = config.BLUE_FALLBACK_MIN_RATE
    delta = abs(float(base_rate) - float(target_rate))
    if delta <= 0:
        return 2 ** 31 - 1                     # 两者不可区分 → 永不触发
    p = min(max(float(base_rate), 1e-9), 1.0 - 1e-9)
    sd = (p * (1.0 - p)) ** 0.5
    return int(np.ceil((z * sd / delta) ** 2))


def _blue_fallback_min_sample() -> int:
    """回退判定所要求的最小样本量（期）。

    优先级：显式配置 config.BLUE_FALLBACK_MIN_SAMPLE (>0) → 统计反推值。
    结果再与 BLUE_FALLBACK_WINDOW 取大，保证修复后只会**更严**、不会更松。
    """
    window = max(int(getattr(config, "BLUE_FALLBACK_WINDOW", 30)), 1)
    explicit = int(getattr(config, "BLUE_FALLBACK_MIN_SAMPLE", 0) or 0)
    if explicit > 0:
        return max(window, explicit)
    return max(window, _min_significant_blue_sample())


def _apply_blue_mode(blue_blend: np.ndarray) -> np.ndarray:
    """按配置把蓝球概率切到目标口径：uniform 模式 / 运行命中率回退 → 均匀 16。

    蓝球单注命中理论基线恒为 1/16 = 6.25%，任何模型都无法提高其长期期望。
    本函数只做一件事：当**样本量足够**且模型长期命中率**统计显著**低于基线时，
    回退均匀 16，避免被一个确实失效的模型带偏。

    修复要点（V4）：旧实现用 BLUE_FALLBACK_WINDOW = 30 期做判定，是**在噪声上
    做决策**。30 期命中率的标准误 = sqrt(0.0625*0.9375/30) ≈ 4.42%，已远大于
    6.25% - 4.5% = 1.75% 的差额——30 期观测到 2/30（6.67%）与「真值 4.5%」或
    「真值 6.25%」都完全相容，p 值远不显著。所以本函数改为：

    1. 由 _blue_fallback_min_sample() 得到最小显著样本量（默认 ≈ 735 期，
       见 _min_significant_blue_sample 的推导）；
    2. 样本量不足 → **恒用覆盖计划**，绝不因短期波动回退；
    3. 样本量达标且命中率仍显著低于阈值 → 才回退均匀 16。

    BLUE_FALLBACK_WINDOW 保留为「至少要看多少期」的下限（向后兼容），真正决定
    是否允许触发的是最小显著样本量；可用 config.BLUE_FALLBACK_MIN_SAMPLE 显式
    覆盖（设 0 走统计反推，不支持放松到低于 window）。
    """
    if config.BLUE_MODE == "uniform":
        return np.full(B_MAX, 1.0 / B_MAX)
    window = max(int(getattr(config, "BLUE_FALLBACK_WINDOW", 30)), 1)
    rate = METH._blue_running_rate(window)
    if rate is None:
        return blue_blend
    min_n = _blue_fallback_min_sample()
    observed = window
    try:
        # _blue_running_rate 按「每期每方法」展开，用同口径估算实际样本量
        rows = METH._per_method_issue_rows(window * 50)
        observed = len(sum(rows.values(), [])) or window
    except Exception:  # noqa: BLE001 —— 样本口径不可得时保守退回窗口期数
        observed = window
    if observed < min_n:
        # 样本不足：不得在噪声上做决策（30 期标准误 4.42% >> 1.75% 差额）
        return blue_blend
    if rate < config.BLUE_FALLBACK_MIN_RATE:
        print(f"[engine] 蓝球模型近 {observed} 期命中率 {rate:.3f} < "
              f"{config.BLUE_FALLBACK_MIN_RATE}（已达最小显著样本量 {min_n}），"
              f"本周期回退均匀 16")
        return np.full(B_MAX, 1.0 / B_MAX)
    return blue_blend


def blue_coverage_plan(n_tickets: int,
                       blue_probs: Optional[np.ndarray] = None) -> List[int]:
    """蓝球覆盖计划：在固定注数预算下，决定 n 注各自应分配的蓝球号。

    ========== 为什么这是本系统唯一真实可得的改进 ==========
    双色球是独立随机事件，**任何算法都无法提高单注命中率**：
      * 红球平均命中恒为 6*6/33 = 1.0909 个/注（与用什么模型选号无关）；
      * 蓝球单注命中恒为 1/16 = 6.25%。
    一等奖需 6 红全中，概率 1/C(33,6) = 1/1_107_568，多注只能线性叠加，
    没有算法空间。所以「提高命中率」是假目标。

    真正可优化的是**多注组合的覆盖结构**：六等奖只需蓝球命中（固定 ¥5）。
    若把 n 注的蓝球**取为互不相同**，则「至少中一个蓝球」的概率从
    「n 次独立重复」变成「n 个不同号覆盖」：

        随机重复（可重复抽样）：P = 1 - (15/16)^n
        去重覆盖（本函数）    ：P = min(n, 16) / 16

    ========== 概率对照表（至少中 1 个蓝球） ==========
        n    随机重复 1-(15/16)^n   去重覆盖 min(n,16)/16   相对提升
        1         6.25%                  6.25%               0%   ← 单注无任何优化空间
        2        12.11%                 12.50%              +3.2%
        3        17.61%                 18.75%              +6.5%
        5        27.58%                 31.25%             +13.3%
       10        47.55%                 62.50%             +31.4%
       16        64.39%                100.00%             +55.3%
       20        72.47%                100.00%            (已饱和，仅成本翻倍)

    ========== 诚实结论（必须明示，不可省略） ==========
    覆盖优化提高的是**中奖频率（体感）**，**不是期望收益**：
      * 六等奖期望返奖比 = (5 * p) / (2 * n) **恒为 0.156（15.6%）**，与 n 无关
        ——因为 p 与 n 同步增长，成本也同步增长，覆盖改变的是分布而非期望；
      * n = 16 覆盖满 16 个蓝球时 p = 100%，但成本 16 注 = ¥32，而六等奖只有
        ¥5，**必然亏本 ¥27**（净回报 -84.4%）；
      * 一/二等奖为浮动奖金且概率极小，任何覆盖方案都不能把负期望变成正期望。
    因此正确口径是：**同样 10 注，「中个蓝球（¥5）」的概率从 47.6% 提到 62.5%**，
    而不是「更容易赚钱」。理性购彩。

    ========== 选取规则 ==========
    按 blue_probs **降序**取号，因此既是「最多不同号」也是「概率最高的一组号」；
    n > 16 时蓝球只有 16 个可用，超出部分按概率加权循环重复，并**优先补齐当前
    出现次数最少的号**，让重复在 16 个号之间尽量均匀。
    blue_probs 为 None / 形状非 (16,) / 含非有限值 / 和 <= 0 时回退均匀 1/16 权重。

    与旧 `_pick_blue_coverage` 的区别：旧逻辑是在**候选票池里挑**，候选不足时只能
    重复，覆盖数是候选池大小的副产物；新逻辑**先定蓝球计划，再配对红球**，
    覆盖数只由 n 决定，结构性最优。
    """
    if n_tickets <= 0:
        return []
    k = int(n_tickets)
    probs: Optional[np.ndarray]
    try:
        probs = np.asarray(blue_probs, dtype=float) if blue_probs is not None else None
    except (TypeError, ValueError):
        probs = None
    if probs is None or probs.shape != (B_MAX,) or not np.all(np.isfinite(probs)) \
            or float(probs.sum()) <= 0:
        probs = np.full(B_MAX, 1.0 / B_MAX)
    else:
        probs = np.clip(probs, 0.0, None)
        if float(probs.sum()) <= 0:
            probs = np.full(B_MAX, 1.0 / B_MAX)
    # 降序排号：概率最高的号先上；n <= 16 时天然互不相同
    order = [int(i) + 1 for i in np.argsort(-probs, kind="stable")]
    plan = order[:min(k, B_MAX)]
    if k > B_MAX:
        # 超出 16 注：按概率加权重复，优先补齐当前出现次数最少的号
        counts: Dict[int, int] = {b: 1 for b in plan}
        for _ in range(k - B_MAX):
            b = min(order, key=lambda x: (counts.get(x, 0), -float(probs[x - 1]), x))
            counts[b] = counts.get(b, 0) + 1
            plan.append(b)
    return plan


def _plan_blue_coverage(scored: List[Dict], n_tickets: int,
                        blue_probs: Optional[np.ndarray] = None) -> List[Dict]:
    """V4 single 模式：**先定蓝球覆盖计划，再配对红球**。

    实现方式是在候选池里为计划中的每个蓝球挑一注置信度最高的候选，并把该候选的
    蓝球改写为计划值（红球保持不变）。若某计划号的候选不足，则取当前置信度最高且
    未被选用的候选改写。

    候选池比注数还小时（例如候选池只有 10 注却请求 16 注）：**不凭空补齐**（不复制
    已选票），输出注数上限 = 候选池大小 m。但覆盖计划继续生效——此时只取计划的
    前 m 个蓝球，仍保证这 m 注的蓝球**互不相同**，覆盖恰为 min(m, 16) 个号。
    这正是旧 `_pick_blue_coverage` 做不到的：它的覆盖数是候选池里「恰好有哪些蓝球」
    的副产物，而本函数的覆盖数只由 m 决定。

    因此「至少中一个蓝球」的概率严格等于 min(m, 16)/16（m = 实际输出注数）。
    """
    if n_tickets <= 0 or not scored:
        return []
    # 候选池大小即输出注数上限：不复制已选票、不凭空补齐
    m = min(int(n_tickets), len(scored))
    plan = blue_coverage_plan(m, blue_probs)
    ordered = sorted(scored, key=lambda x: -float(x.get("confidence", 0.0)))
    picked: List[Dict] = []
    chosen: set = set()
    for b in plan:
        if len(picked) >= m:
            break
        hit = next((t for t in ordered if t["blue"] == b and id(t) not in chosen), None)
        if hit is None:
            # 候选池里没有该蓝球 → 取当前置信度最高且未被选用的候选，改写其蓝球
            hit = next((t for t in ordered if id(t) not in chosen), None)
        if hit is None:
            break                      # 候选池耗尽：不复制已选票
        chosen.add(id(hit))
        t = dict(hit)
        t["blue"] = int(b)
        t["blue_planned"] = True
        picked.append(t)
    return picked


def _pick_blue_coverage(scored: List[Dict], n_tickets: int) -> List[Dict]:
    """picked 阶段：优先让 n 注尽量覆盖 n 个不同蓝球（U2 蓝球覆盖优先）。

    先按置信度降序尽量每个蓝球只取 1 注铺满 n；铺不满（候选池不足）再放宽
    允许同蓝重复补齐（与旧逻辑同蓝上限 max(2, n//5) 对齐兜底）。

    注：**同一张候选票绝不可被选两次**。候选池按 (reds, blue) 去重后，
    若放宽同蓝上限时重新遍历到已选中的票，会凭空复制出重复注——既浪费投注
    金额，也虚增注数（实测 10 注档可产出 3 注重复）。故第二遍按对象身份
    跳过已选票。
    """
    if n_tickets <= 0:
        return []
    if not scored:
        return []
    ordered = sorted(scored, key=lambda x: -x["confidence"])
    picked: List[Dict] = []
    used_blue = set()
    # 第一遍：每个蓝球取置信度最高的一注
    for t in ordered:
        if len(picked) >= n_tickets:
            break
        if t["blue"] in used_blue:
            continue
        used_blue.add(t["blue"])
        picked.append(t)
    # 第二遍：不足则放宽同蓝上限补齐（跳过已选票，避免重复）
    if len(picked) < n_tickets:
        cap = max(2, n_tickets // 5)
        blue_count: Dict[int, int] = {}
        for t in picked:
            blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
        chosen = {id(t) for t in picked}
        for t in ordered:
            if len(picked) >= n_tickets:
                break
            if id(t) in chosen:          # 已选过的候选票不再重复计入
                continue
            if t["blue"] in used_blue and blue_count.get(t["blue"], 0) >= cap:
                continue
            if t["blue"] not in used_blue:
                used_blue.add(t["blue"])
            blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
            chosen.add(id(t))
            picked.append(t)
    return picked[:n_tickets]


def _pick_legacy_blue(scored: List[Dict], n_tickets: int) -> List[Dict]:
    """旧版蓝球分散：同蓝上限 max(2, n//5)。仅在 LOTT_BLUE_COVER=0 时使用（向后兼容）。"""
    if n_tickets <= 0 or not scored:
        return []
    ordered = sorted(scored, key=lambda x: -x["confidence"])
    picked: List[Dict] = []
    blue_count = {}
    for t in ordered:
        if len(picked) >= n_tickets:
            break
        if blue_count.get(t["blue"], 0) >= max(2, n_tickets // 5):
            continue
        blue_count[t["blue"]] = blue_count.get(t["blue"], 0) + 1
        picked.append(t)
    return picked
