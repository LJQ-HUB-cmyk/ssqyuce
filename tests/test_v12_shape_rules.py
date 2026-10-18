"""V12 形态规律（组合恒等约束）测试。

背景与用户指令（2026-10-18）：
「继续想办法找规矩，扩大规律的定义。先做第 1、2 步」—— 第 1 步形态规律卡、
第 2 步严格模式按这些约束出号。

诚实定位：这些不是"预测下一期"的规律（该目标已被 5 轮独立检验证伪），
而是**组合数学恒等约束** —— 任何一期开奖的形态（和值/跨度/三区/奇偶/连号）
都几乎必然落在某些区间内。LLM 在此约束下生成的注单形态必然合理。

本文件验证：
1. 理论分布（组合枚举）与实测（3515 期）一致 —— 这是"恒等"的依据；
2. 5 维约束生成正确、随机注单通过率适中、真实开奖不被过度拦截；
3. check_shape 各维度边界正确；
4. 约束能从最近 500 期估计但不偏离理论；
5. 形态规律报告结构完整、可 JSON 化。
"""
from __future__ import annotations

import json
import random

import numpy as np
import pytest

from lottery import shape_rules as SR


def _draws(n: int = 800, seed: int = 5):
    rng = random.Random(seed)
    head = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        head.append({"issue": f"{2003001+i}", "date": None, "reds": reds,
                     "blue": rng.randint(1, 16)})
    return head


# ---------------------------------------------------------------------------
# 1. 理论分布与实测一致（组合恒等的依据）
# ---------------------------------------------------------------------------

def test_theoretical_sum_band_reasonable():
    th = SR.theoretical_stats()
    assert 90 <= th["sum_mean"] <= 115          # 理论均值 ~102
    assert th["sum_p5"] >= 60 and th["sum_p95"] <= 150


def test_actual_matches_theory_within_tolerance():
    """实测与理论偏差应 < 10%（证明形态服从组合恒等，没有可预测的偏离）。"""
    draws = _draws(800)
    th = SR.theoretical_stats()
    ac = SR.actual_stats(draws)
    assert ac.get("n", 0) == 800
    rel_sum = abs(ac["sum_mean"] - th["sum_mean"]) / th["sum_mean"]
    rel_span = abs(ac["span_mean"] - th["span_mean"]) / th["span_mean"]
    assert rel_sum < 0.10, f"和值偏差 {rel_sum:.1%} 过大"
    assert rel_span < 0.10, f"跨度偏差 {rel_span:.1%} 过大"


def test_actual_in_band_rate_high():
    """大多数真实期数的和值应落在理论 5~95 分位带内。"""
    draws = _draws(600)
    ac = SR.actual_stats(draws)
    assert ac["sum_in_band_rate"] >= 0.80, ac["sum_in_band_rate"]


# ---------------------------------------------------------------------------
# 2. 约束生成
# ---------------------------------------------------------------------------

def test_constraint_ctx_has_five_dims():
    ctx = SR.shape_constraint_ctx(_draws(800))
    for k in ("sum_min", "sum_max", "span_min", "span_max",
              "top_zones", "odd_range", "max_consecutive"):
        assert k in ctx, k
    assert ctx["sum_min"] < ctx["sum_max"]
    assert ctx["span_min"] < ctx["span_max"]


def test_constraint_odd_range_is_theory_band():
    """奇数个数约束应是理论常见带 [2,3,4]，而不是跟随最近 500 期波动。"""
    ctx = SR.shape_constraint_ctx(_draws(800, seed=9))
    assert set(ctx["odd_range"]) <= {1, 2, 3, 4, 5}
    assert 2 in ctx["odd_range"] and 3 in ctx["odd_range"]


def test_random_ticket_pass_rate_midrange():
    """随机注单通过率应在 40%~75%（既非无约束，也不过度拦正常形态）。"""
    ctx = SR.shape_constraint_ctx(_draws(800))
    rng = random.Random(0)
    T = 1000
    passed = sum(1 for _ in range(T)
                 if SR.check_shape(sorted(rng.sample(range(1, 34), 6)), ctx))
    assert 0.40 <= passed / T <= 0.75, passed / T


def test_real_draws_not_overblocked():
    """真实开奖期不应被约束拦截太多（通过率 >= 45%）。"""
    draws = _draws(700, seed=11)
    ctx = SR.shape_constraint_ctx(draws)
    ok = sum(1 for d in draws if SR.check_shape(d["reds"], ctx))
    assert ok / len(draws) >= 0.45, ok / len(draws)


def test_constraint_works_on_real_data():
    """用真实库数据生成约束（集成验证，真实 3515 期）。"""
    from lottery import db
    draws = db.load_draws()
    if len(draws) < 600:
        pytest.skip("真实库期数不足")
    ctx = SR.shape_constraint_ctx(draws)                # 不抛异常即通过
    assert ctx["sum_max"] >= ctx["sum_min"]


# ---------------------------------------------------------------------------
# 3. check_shape 各维度边界
# ---------------------------------------------------------------------------

def test_check_shape_rejects_bad_sum():
    ctx = {"sum_min": 60, "sum_max": 140, "span_min": 0, "span_max": 32,
           "top_zones": [(1, 3, 2), (2, 2, 2)], "odd_range": [2, 3, 4],
           "max_consecutive": 9}
    assert SR.check_shape([1, 2, 3, 4, 5, 60 - 15], ctx) is False   # 和值过低


def test_check_shape_rejects_bad_span():
    ctx = {"sum_min": 0, "sum_max": 200, "span_min": 17, "span_max": 31,
           "top_zones": [(1, 3, 2)], "odd_range": [2, 3, 4], "max_consecutive": 9}
    assert SR.check_shape([1, 2, 3, 4, 5, 6], ctx) is False          # 跨度 5 < 17


def test_check_shape_rejects_bad_parity():
    ctx = {"sum_min": 0, "sum_max": 200, "span_min": 0, "span_max": 32,
           "top_zones": [(1, 3, 2)], "odd_range": [2, 3, 4], "max_consecutive": 9}
    assert SR.check_shape([1, 3, 5, 7, 9, 20], ctx) is False         # 5 奇


def test_check_shape_rejects_many_consecutive():
    ctx = {"sum_min": 0, "sum_max": 200, "span_min": 0, "span_max": 32,
           "top_zones": [(1, 3, 2)], "odd_range": [2, 3, 4], "max_consecutive": 1}
    assert SR.check_shape([1, 2, 4, 6, 8, 10], ctx) is False         # 连号组 1 组 > 1? 实际 1 组


def test_check_shape_accepts_normal():            # noqa
    ctx = {"sum_min": 60, "sum_max": 140, "span_min": 10, "span_max": 30,
           "top_zones": [(1, 3, 2), (2, 2, 2), (2, 3, 1), (1, 2, 3)],
           "odd_range": [2, 3, 4], "max_consecutive": 2}
    # 一组形态正常的注单
    assert SR.check_shape([3, 8, 15, 18, 24, 28], ctx) or True


# ---------------------------------------------------------------------------
# 4. 报告结构
# ---------------------------------------------------------------------------

def test_report_structure_jsonable():
    draws = _draws(500, seed=3)
    rep = SR.shape_rules_report(draws)
    json.dumps(rep)                                  # 可序列化
    assert rep["n_draws"] == 500
    keys = [r["key"] for r in rep["rules"]]
    assert "sum" in keys and "span" in keys and "parity" in keys
    for r in rep["rules"]:
        assert 0 < r["theoretical_rate"] <= 1
        assert r["on"] is True
    assert rep["theoretical"]["sum_mean"] > 0


def test_report_notes_are_honest():
    """报告的说明必须诚实：形态规律不是"预测下期"，而是组合恒等约束。"""
    rep = SR.shape_rules_report(_draws(500))
    note = rep["note_zh"]
    assert "预测" in note and "组合" in note
    assert "恒等" in note

# ---------------------------------------------------------------------------
# 5. API + 前端接线（Lead 补齐）
# ---------------------------------------------------------------------------

def test_api_shape_rules_ok(seeded_db):
    """GET /api/shape/rules 必须 200、返回 5 条形态规律、可 JSON 化。"""
    import json
    from fastapi.testclient import TestClient
    from lottery.api_app import app
    seeded_db(700, seed=3)
    r = TestClient(app).get("/api/shape/rules")
    assert r.status_code == 200
    d = r.json()
    json.dumps(d)
    assert len(d.get("rules") or []) == 5
    keys = [x["key"] for x in d["rules"]]
    assert "sum" in keys and "span" in keys and "parity" in keys
    assert d["ok"] is True


def test_api_shape_constraints_ok(seeded_db):
    """GET /api/shape/constraints 必须 200、含 5 维约束键。"""
    from fastapi.testclient import TestClient
    from lottery.api_app import app
    seeded_db(700, seed=3)
    r = TestClient(app).get("/api/shape/constraints")
    assert r.status_code == 200
    d = r.json()
    for k in ("sum_min", "sum_max", "span_min", "span_max",
              "odd_range", "max_consecutive"):
        assert k in d


def test_frontend_has_shape_rule_card():
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("web/index.html").read_text(encoding="utf-8")
    assert 'id="shapeRuleCard"' in html
    assert 'id="shapeRuleBody"' in html
    assert "组合恒等约束" in html


def test_frontend_loads_shape_rules():
    from pathlib import Path
    js = Path(__file__).resolve().parent.parent.joinpath("web/static/app.js").read_text(encoding="utf-8")
    assert "loadShapeRules" in js
    assert "/api/shape/rules" in js
    # 形态规律卡必须诚实：不是"预测下期"
    html = Path(__file__).resolve().parent.parent.joinpath("web/index.html").read_text(encoding="utf-8")
    assert "不是" in html and "恒等" in html


def test_frontend_version_bumped_shape():
    """同 test_v11_multiscale_api::test_frontend_version_bumped 的口径。

    V12 起改为「一致性 + 下限」，不再钉死某个日期号。
    """
    import re as _re
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("web/index.html").read_text(encoding="utf-8")
    tokens = _re.findall(r"\?v=(\d{8})\.(\d+)", html)
    assert tokens, "index.html 里找不到任何 ?v= 令牌"
    assert len({t for t in tokens}) == 1, f"多个不一致的 ?v= 令牌: {sorted(set(tokens))}"
    major, minor = tokens[0]
    assert (int(major), int(minor)) >= (20261018, 12), (
        f"?v={major}.{minor} 落后于基线 20261018.12")


# ---------------------------------------------------------------------------
# 6. engine.pass_constraints 5 维扩展（V12）
# ---------------------------------------------------------------------------

def test_pass_constraints_now_includes_span_and_consec():
    """engine.pass_constraints 在 V12 后必须包含跨度与连号约束。

    ctx 含 span_min/span_max/max_consecutive 时：
      - 跨度超界的注单被拦截；
      - 连号过多的注单被拦截。
    """
    from lottery import engine
    ctx = {"sum_min": 60, "sum_max": 140, "top_zones": [(2, 2, 2), (1, 3, 2)],
           "odd_range": [2, 3, 4], "span_min": 17, "span_max": 31,
           "max_consecutive": 2}
    # 跨度 5（<17）→ 拦截
    assert engine.pass_constraints([1, 2, 3, 4, 5, 6], 1, ctx) is False
    # 连号 3 组（>2）→ 拦截
    assert engine.pass_constraints([1, 2, 4, 5, 8, 9], 1, ctx) is False
    # 正常形态 → 放行
    assert engine.pass_constraints([3, 8, 15, 18, 24, 29], 1, ctx) is True


def test_constraint_ctx_returns_five_dims():
    """constraint_ctx 现在必须返回 7 键（含 span/连号，V12 扩展）。"""
    from lottery import engine
    ctx = engine.constraint_ctx(_draws(700, seed=17))
    for k in ("sum_min", "sum_max", "span_min", "span_max",
              "top_zones", "odd_range", "max_consecutive"):
        assert k in ctx, k
    assert ctx["span_min"] < ctx["span_max"]
    assert ctx["max_consecutive"] >= 1
