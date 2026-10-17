"""V4-C 覆盖优化 + 奖金期望最大化测试。

核心命题（本文件的诚实性契约）：
  1. 双色球独立随机，**单注命中率不可被算法提高**（n=1 时与随机单注完全一致）；
  2. 唯一真实可得的改进是**固定注数下的蓝球覆盖结构**：
     「至少中 1 个蓝球」概率从 1-(15/16)^n 提升到 min(n,16)/16；
  3. 覆盖优化提高的是**中奖频率**，**不是期望收益**——六等奖 EV 比恒为 0.156，
     且 n=16 覆盖满 16 个蓝球时成本 ¥32 > 六等奖 ¥5，**必然亏本**。
任何一条被"优化"掉，本文件都会红。
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest

B = 16


def _mk_draws(n=800, seed=7):
    """合成 n 期随机开奖（独立均匀，双色球真实口径）。"""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"{2026001 + i}",
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "reds": reds,
            "blue": rng.randint(1, 16),
            "order": reds,
        })
    return out


# ---------------------------------------------------------------------------
# 1. 蓝球覆盖计划：n<=16 必须全部互不相同
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", list(range(1, 17)))
def test_blue_plan_distinct_for_n_up_to_16(n):
    """n = 1..16：蓝球计划必须**全部互不相同**（覆盖结构性最优）。"""
    from lottery import engine as E
    plan = E.blue_coverage_plan(n, np.full(B, 1.0 / B))
    assert len(plan) == n
    assert len(set(plan)) == n, f"n={n} 时应覆盖 {n} 个不同蓝球"
    assert all(1 <= b <= B for b in plan)


def test_blue_plan_covers_all_16_at_n16():
    """n=16：必须覆盖满全部 16 个蓝球（至少中 1 蓝概率 = 100%）。"""
    from lottery import engine as E
    plan = E.blue_coverage_plan(16, np.full(B, 1.0 / B))
    assert sorted(plan) == list(range(1, B + 1))


def test_blue_plan_n_gt_16_repeats_evenly():
    """n>16：蓝球只有 16 个，只能重复；重复必须尽量均匀（不堆在同一个号上）。"""
    from lottery import engine as E
    for n in (17, 20, 32):
        plan = E.blue_coverage_plan(n, np.full(B, 1.0 / B))
        assert len(plan) == n
        assert len(set(plan)) == B, f"n={n} 仍应覆盖全部 16 个蓝球"
        counts = [plan.count(b) for b in range(1, B + 1)]
        assert max(counts) - min(counts) <= 1, f"n={n} 重复分布不均: {sorted(counts)}"


def test_blue_plan_follows_probability_order():
    """计划按 blue_probs 降序取号：概率最高的号必须排在计划最前面。"""
    from lottery import engine as E
    probs = np.full(B, 0.01)
    probs[6] = 0.5      # 7 号最高
    probs[10] = 0.3     # 11 号次高
    plan = E.blue_coverage_plan(5, probs)
    assert plan[0] == 7
    assert plan[1] == 11
    assert len(set(plan)) == 5


# ---------------------------------------------------------------------------
# 2. 边界：n=0 / blue_probs 和为 0 / 形状错误
# ---------------------------------------------------------------------------

def test_blue_plan_edge_cases():
    from lottery import engine as E
    # n = 0
    assert E.blue_coverage_plan(0, np.full(B, 1.0 / B)) == []
    assert E.blue_coverage_plan(-3, np.full(B, 1.0 / B)) == []
    # blue_probs 全 0（和为 0）→ 回退均匀权重，仍必须是有效计划
    plan = E.blue_coverage_plan(10, np.zeros(B))
    assert len(plan) == 10 and len(set(plan)) == 10
    # blue_probs = None → 均匀回退
    plan = E.blue_coverage_plan(6, None)
    assert len(plan) == 6 and len(set(plan)) == 6
    # 形状错误 / 含 nan → 均匀回退（不得抛异常）
    assert len(E.blue_coverage_plan(4, np.full(33, 1.0 / 33))) == 4
    bad = np.full(B, 1.0 / B)
    bad[0] = np.nan
    assert len(E.blue_coverage_plan(4, bad)) == 4


# ---------------------------------------------------------------------------
# 3. 覆盖概率：与手算的 min(n,16)/16 和 1-(15/16)^n 一致
# ---------------------------------------------------------------------------

def _p_naive(n: int) -> float:
    return 1.0 - (15.0 / 16.0) ** n


def test_blue_hit_probability_matches_hand_computed_values():
    """至少中 1 蓝：覆盖 = min(n,16)/16；随机重复 = 1-(15/16)^n。"""
    from lottery import engine as E
    # 覆盖口径（手算参考值）
    assert E._portfolio_blue_hit_prob(list(range(1, 6))) == pytest.approx(5 / 16)
    assert E._portfolio_blue_hit_prob(list(range(1, 11))) == pytest.approx(10 / 16)
    assert E._portfolio_blue_hit_prob(list(range(1, 17))) == pytest.approx(1.0)
    # 随机重复口径
    assert _p_naive(1) == pytest.approx(0.0625, abs=1e-9)
    assert _p_naive(5) == pytest.approx(0.2758, abs=1e-4)
    assert _p_naive(10) == pytest.approx(0.4755, abs=1e-4)
    assert _p_naive(16) == pytest.approx(0.6439, abs=1e-4)
    # 覆盖严格优于（或等于）随机重复
    for n in (2, 3, 5, 10, 16):
        assert n / 16 >= _p_naive(n)
    # 空组合
    assert E._portfolio_blue_hit_prob([]) == 0.0


def test_blue_hit_probability_improvement_ladder():
    """提升阶梯：n=5 → 31.25%，n=10 → 62.50%，n=16 → 100%。"""
    from lottery import engine as E
    plan5 = E.blue_coverage_plan(5)
    plan10 = E.blue_coverage_plan(10)
    plan16 = E.blue_coverage_plan(16)
    assert E._portfolio_blue_hit_prob(plan5) == pytest.approx(0.3125)
    assert E._portfolio_blue_hit_prob(plan10) == pytest.approx(0.6250)
    assert E._portfolio_blue_hit_prob(plan16) == pytest.approx(1.0)
    # 相对提升
    assert (0.3125 - _p_naive(5)) / _p_naive(5) == pytest.approx(0.133, abs=2e-3)
    assert (0.6250 - _p_naive(10)) / _p_naive(10) == pytest.approx(0.314, abs=2e-3)


# ---------------------------------------------------------------------------
# 4. 诚实性自证 A：n=1 期望值与随机单注一致（不虚报能力）
# ---------------------------------------------------------------------------

def test_single_ticket_has_no_optimization_headroom():
    """n=1：奖金期望与随机单注**完全一致**——证明我们没有虚报能力。

    单注红球命中数服从超几何分布 P(r)=C(6,r)C(27,6-r)/C(33,6)，蓝球恒 1/16，
    与选号算法无关。覆盖计划在 n=1 时也没有任何可做的事（只能取 1 个蓝球）。
    """
    from lottery import engine as E
    one = E.expected_prize([1, 2, 3, 4, 5, 6], 7)
    # 红球命中分布必须是超几何（与具体选了哪 6 个红球无关）
    from math import comb
    total = comb(33, 6)
    for r in range(7):
        assert one["prob_by_red_hits"][r] == pytest.approx(
            comb(6, r) * comb(27, 6 - r) / total)
    # 红球平均命中恒为 6*6/33
    mean_r = sum(r * p for r, p in one["prob_by_red_hits"].items())
    assert mean_r == pytest.approx(6 * 6 / 33, abs=1e-9)
    # 蓝球命中恒为 1/16
    assert one["blue_hit_prob"] == pytest.approx(1 / 16)
    # 换一组完全不同的红球，期望值必须一模一样（算法无法改变它）
    other = E.expected_prize([28, 29, 30, 31, 32, 33], 11)
    assert other["expected_reward"] == pytest.approx(one["expected_reward"])
    # n=1 的组合期望 = 单注期望，且蓝球覆盖计划只有 1 个号
    pf = E.portfolio_expected_value([{"reds": [1, 2, 3, 4, 5, 6], "blue": 7}])
    assert pf["expected_reward"] == pytest.approx(one["expected_reward"])
    assert E.blue_coverage_plan(1) == E.blue_coverage_plan(1)
    assert len(E.blue_coverage_plan(1)) == 1


def test_expected_value_scales_linearly_with_ticket_count():
    """同注数下期望奖金只随注数线性增长——覆盖优化不改变期望。"""
    from lottery import engine as E
    base = E.expected_prize([1, 2, 3, 4, 5, 6], 7)["expected_reward"]
    tickets = [{"reds": [1, 2, 3, 4, 5, 6], "blue": b} for b in range(1, 11)]
    pf = E.portfolio_expected_value(tickets)
    assert pf["expected_reward"] == pytest.approx(base * 10)
    assert pf["cost"] == pytest.approx(20.0)
    assert pf["tickets"] == 10


# ---------------------------------------------------------------------------
# 5. 诚实性自证 B：覆盖满 16 蓝必然亏本（成本 ¥32 > 六等奖 ¥5）
# ---------------------------------------------------------------------------

def test_sixteenth_blue_coverage_is_a_guaranteed_loss():
    """覆盖满 16 个蓝球 → 必然中六等奖 ¥5，但成本 16×¥2=¥32，净亏 ¥27。

    这是本项目最重要的诚实边界：覆盖优化把"中奖频率"提到 100%，
    却**同时**把成本提到远高于该奖级奖金——频率 ≠ 收益。
    """
    from lottery import engine as E
    plan = E.blue_coverage_plan(16)
    assert len(set(plan)) == 16
    tickets = [{"reds": [1, 2, 3, 4, 5, 6], "blue": b} for b in plan]
    pf = E.portfolio_expected_value(tickets)
    # 至少中 1 蓝 = 100% → 六等奖必中一次 = ¥5
    assert pf["blue_hit_prob"] == pytest.approx(1.0)
    sixth = 5.0 * pf["blue_hit_prob"]
    assert sixth == pytest.approx(5.0)
    # 成本 ¥32 > 六等奖 ¥5 → 必然亏本
    assert pf["cost"] == pytest.approx(32.0)
    assert pf["cost"] > sixth
    assert pf["cost"] - sixth == pytest.approx(27.0)
    # 「六等奖净额」口径明确为负
    assert (sixth - pf["cost"]) / pf["cost"] == pytest.approx(-0.84375)


def test_sixth_prize_ev_ratio_is_constant_0156():
    """六等奖 EV 比 = 5·p / (2n) 恒为 0.156，与 n 无关——覆盖只改频率不改期望。"""
    from lottery import engine as E
    for n in (1, 5, 10, 16):
        plan = E.blue_coverage_plan(n)
        p = E._portfolio_blue_hit_prob(plan)
        ratio = (5.0 * p) / (2.0 * n)
        assert ratio == pytest.approx(5.0 / 32.0, abs=1e-12), f"n={n} EV 比应恒为 0.15625"
    assert 5.0 / 32.0 == pytest.approx(0.15625)


def test_portfolio_ev_ratio_is_below_one():
    """整组期望返奖比 < 1（彩票为负期望游戏），系统必须如实展示。"""
    from lottery import engine as E
    tickets = [{"reds": [1, 2, 3, 4, 5, 6], "blue": b} for b in E.blue_coverage_plan(10)]
    pf = E.portfolio_expected_value(tickets)
    assert pf["ev_ratio"] < 1.0
    assert pf["net"] < 0
    # 浮动奖金假设必须显式标注，不得假装知道真实值
    assert "jackpot_assumption" in pf
    assert pf["jackpot_assumption"]["first"] > 0
    assert "浮动" in pf["jackpot_assumption"]["source"]


def test_expected_prize_prize_levels_match_official_rules():
    """奖级概率与官方规则一致：一等奖 = P(6红)×1/16 = 1/C(33,6)/16。"""
    from lottery import engine as E
    from math import comb
    one = E.expected_prize([1, 2, 3, 4, 5, 6], 7)
    p = one["prob_by_level"]
    total = comb(33, 6)
    # 一等奖：6 红全中 + 蓝中
    assert p[1] == pytest.approx((1 / total) * (1 / 16))
    # 二等奖：6 红全中 + 蓝不中
    assert p[2] == pytest.approx((1 / total) * (15 / 16))
    # 各奖级概率之和 = 中奖概率，且必须 < 1
    assert sum(p.values()) < 1.0
    # 六等奖（蓝中但红球命中 <= 2）概率最大
    assert p[6] > p[5] > p[4]
    # 系统口径下 1/C(33,6) 恰为 1/1107568
    assert total == 1107568

    # 与 evaluate.prize_level 的判定逻辑交叉验证：随机抽样比对
    from lottery import evaluate as EV
    rng = random.Random(3)
    for _ in range(200):
        pred = sorted(rng.sample(range(1, 34), 6))
        act = sorted(rng.sample(range(1, 34), 6))
        pb, ab = rng.randint(1, 16), rng.randint(1, 16)
        assert EV.prize_level(pred, pb, act, ab) in range(0, 7)


# ---------------------------------------------------------------------------
# 6. 回退逻辑：样本不足不触发，样本充足才触发
# ---------------------------------------------------------------------------

def test_min_significant_sample_derivation():
    """最小显著样本量 = ceil((1.96·sd/delta)^2) ≈ 735 期，远大于 30。"""
    from lottery import engine as E
    n = E._min_significant_blue_sample()
    assert 700 <= n <= 800, f"手算约 735 期，实际 {n}"
    # 30 期标准误 4.42% 已远超 1.75% 的差额 → 30 期判定无意义
    se30 = math.sqrt(0.0625 * 0.9375 / 30)
    assert se30 > (0.0625 - 0.045)
    assert E._blue_fallback_min_sample() >= 500


def test_no_fallback_with_insufficient_sample_30_periods(tmp_path, monkeypatch):
    """30 期低命中数据 → **不触发**回退（样本不足，是噪声不是信号）。"""
    from lottery import config, db, engine as E
    # 用 monkeypatch 而非直接赋值：否则 config.DB_PATH 在用例结束后仍指向这个
    # 临时空库，污染后续走 db.load_draws() 的用例（曾让别文件的用例读到空库而假红）。
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db.sqlite")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.close()
    # 30 期里仅 1 次蓝球命中（3.3% < 4.5% 阈值）——旧逻辑会回退，新逻辑不应回退
    for i in range(30):
        db.get_conn().execute(
            """INSERT INTO eval_details
               (issue,method,seq,red_hits,blue_hit,prize_level,reward,ticket_cost,net_return,evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"2026{i:04d}", "stat:freq", 1, 0, 1 if i == 0 else 0, 0, 0.0, 2.0, -2.0, 1.0))
    db.get_conn().commit()
    sentinel = np.full(16, 1.0 / 8)      # 非均匀分布：若回退会被替换成 1/16
    out = E._apply_blue_mode(sentinel)
    assert np.allclose(out, sentinel), "30 期样本不足，不得在噪声上回退"
    db.close()


def test_fallback_triggers_with_large_biased_sample(tmp_path, monkeypatch):
    """800 期强偏置（0 命中）数据 → 样本量达标，触发回退均匀 16。"""
    from lottery import config, db, engine as E
    # 同上：用 monkeypatch 管理临时库，避免把空库路径泄漏给后续用例
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data2")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db2.sqlite")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.close()
    conn = db.get_conn()
    for i in range(800):
        conn.execute(
            """INSERT INTO eval_details
               (issue,method,seq,red_hits,blue_hit,prize_level,reward,ticket_cost,net_return,evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"2026{i:05d}", "stat:freq", 1, 0, 0, 0, 0.0, 2.0, -2.0, 1.0))
    conn.commit()
    sentinel = np.full(16, 1.0 / 8)
    out = E._apply_blue_mode(sentinel)
    assert np.allclose(out, 1.0 / 16), "样本量达标且命中率显著低于阈值，应回退均匀 16"
    db.close()


def test_fallback_respects_explicit_min_sample(tmp_path, monkeypatch):
    """显式 BLUE_FALLBACK_MIN_SAMPLE 可控制门槛（且不会低于 WINDOW）。"""
    from lottery import config, db, engine as E
    # 同上：monkeypatch 管理临时库（此处原为直接赋值，会泄漏 DB_PATH）
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data3")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db3.sqlite")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.close()
    monkeypatch.setattr(config, "BLUE_FALLBACK_MIN_SAMPLE", 3)
    monkeypatch.setattr(config, "BLUE_FALLBACK_WINDOW", 30)
    for i in range(40):
        db.get_conn().execute(
            """INSERT INTO eval_details
               (issue,method,seq,red_hits,blue_hit,prize_level,reward,ticket_cost,net_return,evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"2026{i:04d}", "stat:freq", 1, 0, 0, 0, 0.0, 2.0, -2.0, 1.0))
    db.get_conn().commit()
    out = E._apply_blue_mode(np.full(16, 1.0 / 8))
    assert np.allclose(out, 1.0 / 16), "显式降低门槛后 40 期样本应可触发回退"
    db.close()


def test_blue_mode_uniform_still_works():
    """uniform 模式仍直接返回均匀分布（回归旧行为）。"""
    from lottery import config, engine as E
    old = config.BLUE_MODE
    config.BLUE_MODE = "uniform"
    try:
        assert np.allclose(E._apply_blue_mode(np.full(16, 1.0 / 8)), 1.0 / 16)
    finally:
        config.BLUE_MODE = old


# ---------------------------------------------------------------------------
# 7. 覆盖优化目标函数：expected_value 为默认，且给优化前后对比
# ---------------------------------------------------------------------------

def _mk_pool(n=200, seed=22):
    rng = random.Random(seed)
    return [{"reds": sorted(rng.sample(range(1, 34), 6)),
             "blue": rng.randint(1, 16),
             "confidence": float(rng.random() * 100),
             "method": "stat:freq"}
            for _ in range(n)]


def test_coverage_objective_expected_value_is_default():
    """objective 默认 expected_value；两种口径都能产出合法预算内注数。"""
    from lottery import engine as E
    pool = _mk_pool()
    for obj in ("expected_value", "coverage"):
        picked = E.coverage_optimize(pool, 10, r=3, trials=300, rng=random.Random(1),
                                     objective=obj)
        assert len(picked) == 10
        assert len({(tuple(t["reds"]), t["blue"]) for t in picked}) == 10
    # 非法 objective 回退默认，不抛异常
    picked = E.coverage_optimize(pool, 5, trials=100, rng=random.Random(2),
                                 objective="nonsense")
    assert len(picked) == 5


def test_coverage_optimize_with_blue_probs_covers_distinct_blues():
    """给定 blue_probs 时，优化结果必须覆盖 10 个不同蓝球（结构性保证）。"""
    from lottery import engine as E
    pool = _mk_pool()
    picked = E.coverage_optimize(pool, 10, trials=300, rng=random.Random(4),
                                 blue_probs=np.full(16, 1 / 16))
    assert len(picked) == 10
    assert len({t["blue"] for t in picked}) == 10


def test_coverage_optimize_compare_reports_before_after():
    """优化前后对比：蓝球覆盖数与覆盖概率必须提升，期望奖金如实展示。"""
    from lottery import engine as E
    pool = _mk_pool()
    cmp = E.coverage_optimize_compare(pool, 10, trials=500, rng=random.Random(5),
                                      blue_probs=np.full(16, 1 / 16))
    assert set(cmp) >= {"before", "after", "improvement", "objective", "jackpot_assumption"}
    assert cmp["objective"] == "expected_value"
    assert cmp["before"]["tickets"] == cmp["after"]["tickets"] == 10
    # 优化后覆盖概率不低于优化前（随机候选池通常严格更高）
    assert cmp["after"]["blue_hit_prob"] >= cmp["before"]["blue_hit_prob"]
    assert cmp["after"]["blue_coverage_count"] >= cmp["before"]["blue_coverage_count"]
    assert cmp["improvement"]["blue_hit_prob"] >= 0
    # 期望奖金在两种选择下都如实给出（且都是负期望）
    assert cmp["before"]["ev_ratio"] < 1 and cmp["after"]["ev_ratio"] < 1


# ---------------------------------------------------------------------------
# 8. 端到端：predict_next 的覆盖元数据
# ---------------------------------------------------------------------------

def test_predict_next_reports_honest_coverage_metadata():
    """端到端：single 路径产出全覆盖蓝球 + 诚实的 EV/覆盖元数据。"""
    from lottery import engine as E
    draws = _mk_draws(500)
    res = E.predict_next(draws, use_llm=False, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(8))
    assert res["actual_tickets"] == 10
    # 10 注必须覆盖 10 个不同蓝球
    assert res["blue_covered"] == 10
    assert res["blue_hit_prob"] == pytest.approx(0.625)
    assert res["blue_hit_prob_naive"] == pytest.approx(_p_naive(10), abs=1e-6)
    assert res["blue_hit_prob"] > res["blue_hit_prob_naive"]
    pf = res["portfolio_ev"]
    assert pf is not None
    assert pf["cost"] == pytest.approx(20.0)
    assert pf["ev_ratio"] < 1.0, "彩票为负期望，系统不得虚报盈利"
    assert "jackpot_assumption" in pf
    # 输出注不得重复
    keys = [(tuple(t["reds"]), t["blue"]) for t in res["tickets"]]
    assert len(keys) == len(set(keys))


def test_predict_next_coverage_mode_uses_ev_objective():
    """coverage 模式：默认走"EV 守恒"目标，并把目标写进结果元数据。

    V10-00a：目标值由 ``expected_value`` 更名为 ``ev_conserving``（语义更准确：
    该目标下 EV 是恒等的，真正被优化的是冗余度/覆盖结构）。
    旧值 ``expected_value`` 仍通过 ``config.COVERAGE_OBJECTIVE_ALIASES`` 别名可用。
    """
    from lottery import engine as E
    draws = _mk_draws(500)
    res = E.predict_next(draws, use_llm=False, use_ml=False, n_tickets=8,
                         persist=False, rng=random.Random(9), coverage=True)
    assert res["coverage_mode"] is True
    assert res["coverage_objective"] == "ev_conserving"
    assert res["actual_tickets"] == 8
    assert res["blue_covered"] >= 4


def test_predict_next_small_pool_never_duplicates():
    """small 候选池（全部方法关闭）下不得产出重复注（保持既有回归契约）。"""
    from lottery import config, engine as E, methods
    old_mode, old_spec = config.METHOD_MODE, config.METHODS_SPEC
    config.METHOD_MODE = "production"
    config.METHODS_SPEC = methods.implement_spec("-stat,-ml,-llm,-uniform,-blend")
    try:
        draws = _mk_draws(400)
        res = E.predict_next(draws, use_llm=False, use_ml=False, n_tickets=10,
                             persist=False, rng=random.Random(10))
        keys = [(tuple(t["reds"]), t["blue"]) for t in res["tickets"]]
        assert len(keys) == len(set(keys))
    finally:
        config.METHOD_MODE, config.METHODS_SPEC = old_mode, old_spec


# ---------------------------------------------------------------------------
# 9. 覆盖数是「结构性保证」：只由输出注数决定，不由候选池恰好有哪些蓝球决定
# ---------------------------------------------------------------------------

def test_coverage_count_depends_only_on_output_size_not_pool_blues():
    """候选池只有 3 个不同蓝球时，6 注仍必须铺出 6 个不同蓝球。

    这是 V4 与旧 `_pick_blue_coverage` 的本质区别：旧逻辑的覆盖数是「候选池里恰好
    有哪些蓝球」的副产物（池里只有 3 个号 → 最多覆盖 3 个）；新逻辑**先定计划再
    配对红球**，覆盖数 = min(输出注数, 16)。
    """
    from lottery import engine as E
    pool = [{"reds": sorted(random.Random(100 + i).sample(range(1, 34), 6)),
             "blue": [1, 1, 2, 2, 3, 3][i], "confidence": float(90 - i)}
            for i in range(6)]
    plan = E.blue_coverage_plan(6)
    picked = E._plan_blue_coverage(pool, 6, np.full(B, 1.0 / B))
    assert len(picked) == 6
    assert len({t["blue"] for t in picked}) == 6, "6 注必须覆盖 6 个不同蓝球"
    assert set(t["blue"] for t in picked) == set(plan)
    # 同一张候选票绝不出现两次
    keys = [(tuple(t["reds"]), t["blue"]) for t in picked]
    assert len(keys) == len(set(keys))


def test_coverage_plan_survives_pool_smaller_than_request():
    """候选池小于请求注数：不凭空补齐，但覆盖仍 = min(池大小, 16)。"""
    from lottery import engine as E
    rng = random.Random(5)
    pool = [{"reds": sorted(rng.sample(range(1, 34), 6)),
             "blue": rng.randint(1, 16), "confidence": float(rng.random() * 100)}
            for _ in range(10)]
    for req in (10, 16, 20):
        picked = E._plan_blue_coverage(pool, req, np.full(B, 1.0 / B))
        assert len(picked) == 10, "不得补齐到请求注数"
        assert len({t["blue"] for t in picked}) == 10, f"req={req} 时 10 注应覆盖 10 个蓝球"
        keys = [(tuple(t["reds"]), t["blue"]) for t in picked]
        assert len(keys) == len(set(keys)), "不得复制已选票"


def test_planned_coverage_beats_legacy_on_same_pool():
    """同一候选池下，计划覆盖的不同蓝球数 >= 旧逻辑（严格更优或持平）。"""
    from lottery import engine as E
    rng = random.Random(77)
    pool = [{"reds": sorted(rng.sample(range(1, 34), 6)),
             "blue": rng.randint(1, 4),        # 池里只有 4 个不同蓝球
             "confidence": float(rng.random() * 100)}
            for _ in range(40)]
    legacy = E._pick_blue_coverage(pool, 10)
    planned = E._plan_blue_coverage(pool, 10, np.full(B, 1.0 / B))
    assert len({t["blue"] for t in legacy}) <= 4
    assert len({t["blue"] for t in planned}) == 10
    assert len({t["blue"] for t in planned}) > len({t["blue"] for t in legacy})


def test_predict_next_n1_matches_random_single_ticket():
    """端到端 n=1：输出的覆盖概率与随机单注一致（6.25%），不虚报能力。"""
    from lottery import engine as E
    draws = _mk_draws(500)
    res = E.predict_next(draws, use_llm=False, use_ml=False, n_tickets=1,
                         persist=False, rng=random.Random(11))
    assert res["actual_tickets"] == 1
    assert res["blue_covered"] == 1
    assert res["blue_hit_prob"] == pytest.approx(1 / 16)
    assert res["blue_hit_prob"] == pytest.approx(res["blue_hit_prob_naive"])
    pf = res["portfolio_ev"]
    assert pf["tickets"] == 1
    assert pf["cost"] == pytest.approx(2.0)
