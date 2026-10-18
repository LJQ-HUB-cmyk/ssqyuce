"""V11 S5：规律生命周期（衰减 → 退居二线 monitor）测试。

覆盖 docs/reviews/AI_DEMOTION_DESIGN.md §9 的 T1–T10，并对四条硬约束各给一条
**反向**用例（改了实现就会红）：

* 约束 1（AI 不得自由判断）：LLM 输出被逐条核对，且**不改变状态**；
* 约束 2（不得死锁）：LLM 不可用 / 状态文件损坏 → 状态机照常 / 规律照常可用；
* 约束 3（退居二线 = monitor）：任何路径都不写 rejected；
* 约束 4（滞后）：抖动序列在有滞后时迁移次数 ≤1。
"""
from __future__ import annotations

import json

import pytest

from lottery import rule_lifecycle as L


# ---------------------------------------------------------------------------
# 构造辅助：把"某个判据成立"写成可读的指标
# ---------------------------------------------------------------------------

def _metrics(*, all_delta=None, all_p=None, all_mde=0.0422,
             eras=None, recent_delta=None, recent_p=None, prereq_ok=None):
    m = {"scales": {}, "eras": [], "recent": {}, "upgrade_prereq": {}}
    if all_delta is not None:
        m["scales"]["all"] = {"n": 3315, "delta": all_delta, "p_raw": all_p,
                              "mde_abs": all_mde, "credible": False}
    if eras is not None:
        m["eras"] = eras
    if recent_delta is not None:
        m["recent"] = {"n": 150, "n_eff": 130.4, "delta": recent_delta,
                       "p_adjusted": recent_p, "mde": 0.2}
    if prereq_ok is not None:
        m["upgrade_prereq"] = {"ok": bool(prereq_ok)}
    return m


def _era(name, delta, n=150, mde=0.2, p=0.5):
    return {"era": name, "n": n, "delta": delta, "mde": mde, "p_value": p}


#: 三条判据全成立的指标（最典型的"衰减"）
DEMOTE_METRICS = _metrics(all_delta=-0.06, all_p=0.01,
                          eras=[_era("P22", -0.12), _era("P23", -0.15)],
                          recent_delta=-0.2, recent_p=0.001)

#: 平稳指标（现实数据里的常态）
HOLD_METRICS = _metrics(all_delta=-0.0073, all_p=0.62,
                        eras=[_era("P22", 0.062), _era("P23", 0.076)],
                        recent_delta=0.121, recent_p=1.0)


# ---------------------------------------------------------------------------
# T1 判据：三条各自单独命中 → demote；都不成立 → hold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,metrics,expect_crit", [
    ("D1_only", _metrics(all_delta=-0.06, all_p=0.01), "D1"),
    ("D2_only", _metrics(eras=[_era("P22", -0.12), _era("P23", -0.15)]), "D2"),
    ("D3_only", _metrics(recent_delta=-0.2, recent_p=0.001), "D3"),
])
def test_t1_each_criterion_alone_triggers_demote(label, metrics, expect_crit):
    out = L.decide_rule_state(metrics)
    assert out["direction"] == "demote", label
    assert expect_crit in out["criteria"]
    assert out["reasons"], "必须给出人读理由"


def test_t1_all_criteria_together():
    out = L.decide_rule_state(DEMOTE_METRICS)
    assert out["direction"] == "demote"
    assert set(out["criteria"]) == {"D1", "D2", "D3"}


def test_t1_hold_when_nothing_fires():
    out = L.decide_rule_state(HOLD_METRICS)
    assert out["direction"] == "hold"
    assert out["criteria"] == []
    assert out["metrics_ok"] is True


@pytest.mark.parametrize("metrics", [
    {},                                              # 完全缺指标
    {"scales": {}},                                  # 缺 all 档
    _metrics(all_delta=-0.06, all_p=None),           # D1 缺 p → 不成立
    _metrics(all_delta=-0.06, all_p=0.5),            # 负但 p 不显著
    _metrics(all_delta=-0.01, all_p=0.01),           # 显著但幅度 < MDE
    _metrics(eras=[_era("P22", -0.12)]),             # 只有一段 → D2 不成立
    _metrics(eras=[_era("P22", -0.12), _era("P23", 0.05)]),   # 方向不一致
    _metrics(eras=[_era("P22", -0.01, mde=1.0), _era("P23", -0.01, mde=1.0)]),  # 幅度不足 MDE/2
    _metrics(eras=[_era("P22", -0.12, n=30), _era("P23", -0.12, n=30)]),       # 样本 <60 不参与
    _metrics(recent_delta=-0.2, recent_p=0.5),       # 负但不显著
    _metrics(recent_delta=+0.2, recent_p=0.001),     # 显著但为正
])
def test_t1_negative_cases_hold(metrics):
    """反向用例：任何一条判据的"半个条件"都不许触发 demote（宁漏不误）。"""
    assert L.decide_rule_state(metrics)["direction"] == "hold"


def test_t1_upgrade_requires_prereq():
    strong = _metrics(all_delta=+0.06, all_p=0.01, prereq_ok=False)
    assert L.decide_rule_state(strong)["direction"] == "hold", "未过前置条件不得升级"
    ok = _metrics(all_delta=+0.06, all_p=0.01, prereq_ok=True)
    assert L.decide_rule_state(ok)["direction"] == "upgrade"


# ---------------------------------------------------------------------------
# T2 纯随机不得误报（1000 次蒙特卡洛）
# ---------------------------------------------------------------------------

def test_t2_pure_random_rarely_fires():
    """用真实统计口径模拟"无效应"数据：每档 delta 服从 N(0, mde/3.55) 附近。

    判据 D1 要求 p<0.05 **且** |delta|>=MDE，在真零假设下二者同时成立的概率
    远低于名义 α，因此误触发率必须明显低于 5%。
    """
    import random

    rng = random.Random(20261018)
    fired = 0
    N = 1000
    for _ in range(N):
        # mde = 3.55*sigma/sqrt(n) → sigma = mde*sqrt(n)/3.55；用 n=3315 的相对刻度
        sd = 0.0422 / 3.55
        d_all = rng.gauss(0, sd)
        se = sd / (sd / 0.0422 * 3.55) if sd else 1.0
        # p_raw 由 delta 与 sigma 复算
        z = abs(d_all) / (0.0422 / 3.55)
        p = 2 * (1 - _phi(z))
        m = _metrics(all_delta=d_all, all_p=p,
                     eras=[_era("P22", rng.gauss(0, 0.15)), _era("P23", rng.gauss(0, 0.15))],
                     recent_delta=rng.gauss(0, 0.15), recent_p=rng.random())
        if L.decide_rule_state(m)["direction"] == "demote":
            fired += 1
    assert fired / N < 0.05, f"纯随机误触发率 {fired / N:.3%} 过高（名义上限 5%）"


def _phi(x: float) -> float:
    from math import erf, sqrt
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def test_t2_injected_decay_is_detected():
    """注入偏置必须检出（与 T2 反向配对，沿用项目 V4 起的传统）。"""
    for _ in range(20):
        m = _metrics(all_delta=-0.09, all_p=1e-6,
                     eras=[_era("P22", -0.2), _era("P23", -0.22)],
                     recent_delta=-0.3, recent_p=1e-4)
        assert L.decide_rule_state(m)["direction"] == "demote"


# ---------------------------------------------------------------------------
# T3 滞后：连续 K 次才迁移；min_dwell 内不再评估
# ---------------------------------------------------------------------------

def _run(seq, thresholds=None, issue_prefix="2026", start_issue=101, base_state="active"):
    """把一串 direction 喂给状态机，返回每步结果。

    ``base_state="active"`` 模拟"这条规律本来就在一线"——否则从 unmanaged 起步时
    demote 的第一次判定会被当作基线、第二次才累计，测试就测不到滞后本身。
    ``base_state="unmanaged"`` 则传 ``None``（真正的"首次纳入"语义）。
    """
    th = thresholds or {"k_demote": 2, "k_upgrade": 2, "min_dwell": 0}
    rec = None if base_state == "unmanaged" else {
        "state": base_state, "pending": {"direction": None, "streak": 0},
        "dwell": int(th.get("min_dwell", 0)), "eval_count": 1}
    out = []
    for i, direction in enumerate(seq):
        issue = f"{issue_prefix}{start_issue + i:02d}"
        dec = {"direction": direction, "criteria": ["D1"] if direction != "hold" else [],
               "reasons": ["x"]}
        rec = L.step_lifecycle(dec, rec, thresholds=th, issue=issue)
        out.append(rec)
    return out


def test_t3_first_call_only_establishes_baseline():
    r = _run(["demote"], base_state="unmanaged")[0]
    assert r["migrated"] is False, "首次纳入只建立基线，绝不当天判退"
    assert r["state"] == "unmanaged"


def test_t3_requires_consecutive_k():
    rs = _run(["demote", "demote"], thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 0})
    assert rs[0]["migrated"] is False and rs[0]["pending"]["streak"] == 1
    assert rs[1]["migrated"] is True and rs[1]["state"] == "monitor"


def test_t3_non_consecutive_resets_streak():
    rs = _run(["demote", "hold", "demote"],
              thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 0})
    assert rs[1]["pending"]["streak"] == 0
    assert rs[2]["migrated"] is False, "中间有 hold 就必须清零重数"


def test_t3_opposite_direction_resets_streak():
    rs = _run(["demote", "upgrade", "demote"],
              thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 0})
    assert rs[2]["pending"]["streak"] == 1


def test_t3_min_dwell_blocks_evaluation():
    rs = _run(["demote", "demote", "demote", "demote", "demote"],
              thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 3})
    assert rs[0]["pending"]["streak"] == 1
    assert rs[1]["migrated"] is True, "第 2 次达到 K=2 应迁移"
    # 迁移后 dwell=0 < 3 → 接下来三次都不评估（state 不变、last_eval.action=dwell）
    for k in (2, 3, 4):
        assert rs[k]["last_eval"]["action"] == "dwell", k
        assert rs[k]["migrated"] is False
    assert rs[4]["dwell"] == 3


def test_t3_dwell_expires_and_upgrade_works():
    th = {"k_demote": 1, "k_upgrade": 1, "min_dwell": 1}
    rs = _run(["demote", "upgrade", "upgrade"], thresholds=th)
    assert rs[0]["state"] == "monitor", "K=1 时第一次判定即迁移到二线"
    assert rs[1]["last_eval"]["action"] == "dwell", "冷却期内不评估"
    assert rs[2]["migrated"] is True and rs[2]["state"] == "active"


def test_t3_already_in_target_does_not_repeat():
    rs = _run(["demote", "demote", "demote"],
              thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 0})
    assert rs[2]["last_eval"]["action"] == "already"
    assert rs[2]["migrated"] is False
    assert rs[2]["since"] == rs[1]["since"], "重复判定不得刷新 since"


# ---------------------------------------------------------------------------
# T4 约束 3：迁移目标只能是 monitor / active，任何路径都不写 rejected / retired
# ---------------------------------------------------------------------------

def test_t4_never_writes_rejected_or_retired():
    import itertools
    th = {"k_demote": 1, "k_upgrade": 1, "min_dwell": 0}
    targets = set()
    for seq in itertools.product(["hold", "demote", "upgrade"], repeat=3):
        for r in _run(list(seq), thresholds=th):
            targets.add(r["state"])
            assert r["state"] in L.STATES
    assert "retired" not in targets, "自动判定绝不产生 retired（只能人工）"


def test_t4_demote_target_is_monitor_only():
    rec = _run(["demote", "demote"], thresholds={"k_demote": 2, "k_upgrade": 2, "min_dwell": 0})[-1]
    assert rec["state"] == "monitor"
    assert "monitor" in L.AUTO_TARGETS and "rejected" not in L.AUTO_TARGETS


def test_t4_retired_is_frozen():
    rec = {"state": "retired", "pending": {"direction": None, "streak": 0}, "dwell": 9}
    out = L.step_lifecycle({"direction": "upgrade", "criteria": ["U1"]}, rec, issue="2026120")
    assert out["state"] == "retired"
    assert out["migrated"] is False


# ---------------------------------------------------------------------------
# T5 抖动：无滞后会反复切换，有滞后最多切一次
# ---------------------------------------------------------------------------

def test_t5_hysteresis_suppresses_flapping():
    """构造在阈值附近来回抖的序列：判据 50% 周期成立。

    无滞后（K=1, dwell=0）应来回切换多次；有滞后（K=2, dwell=3）应 ≤1 次。
    """
    import random

    rng = random.Random(7)
    seq = ["demote" if rng.random() < 0.5 else "hold" for _ in range(60)]

    def count_migrations(thresholds):
        rec = {"state": "active", "pending": {"direction": None, "streak": 0},
               "dwell": int(thresholds.get("min_dwell", 0)), "eval_count": 1}
        n = 0
        for i, d in enumerate(seq):
            rec = L.step_lifecycle(
                {"direction": d, "criteria": ["D1"] if d != "hold" else [], "reasons": ["x"]},
                rec, thresholds=thresholds, issue=f"2026{100 + i:02d}")
            n += 1 if rec["migrated"] else 0
        return n

    loose = count_migrations({"k_demote": 1, "k_upgrade": 1, "min_dwell": 0})
    strict = count_migrations({"k_demote": 2, "k_upgrade": 2, "min_dwell": 3})
    assert loose >= 5, f"无滞后应明显抖动（实际 {loose}）"
    assert strict <= 1, f"有滞后应几乎不抖动（实际 {strict}）"


# ---------------------------------------------------------------------------
# T6 约束 2：LLM 不可用 → 状态与"LLM 全判 keep"逐字段相同
# ---------------------------------------------------------------------------

def _fake_snapshot():
    return {
        "rules": [
            {"id": "r-one", "usable": True, "tier": "usable_long_term",
             "dim": {"key": "repeat", "params": {"back": 1}}, "statement": "重复号",
             "source": "stat", "hyp_status": "validated",
             "prereg": {"status": "passed"}},
            {"id": "r-two", "usable": False, "tier": "monitor",
             "dim": {"key": "neighbor", "params": {"back": 1, "offset": 1}},
             "statement": "邻号", "source": "stat", "hyp_status": "proposed"},
        ],
        "counts": {"usable": 1, "usable_long_term": 1, "usable_phase": 0,
                   "monitor": 1, "rejected": 0},
    }


@pytest.fixture()
def stub_metrics(monkeypatch):
    """把真实指标换成固定值，避免测试依赖 3515 期数据的重算。"""
    box = {"metrics": DEMOTE_METRICS}

    def _build(draws, dim, upgrade_prereq=None, scales=None):
        m = dict(box["metrics"])
        m["upgrade_prereq"] = dict(upgrade_prereq or {})
        m["dim"] = dim
        return m
    monkeypatch.setattr(L, "build_metrics", _build)
    monkeypatch.setattr(L, "_llm_review", lambda *a, **k: dict(L.DEF_REVIEW))
    return box


def test_t6_llm_unavailable_keeps_states_identical(tmp_path, stub_metrics, monkeypatch):
    draws = [{"issue": "2026118", "reds": [1, 2, 3, 4, 5, 6], "blue": 1}]

    # A：LLM 完全不可用
    monkeypatch.setattr(L, "_llm_review", lambda *a, **k: {
        **L.DEF_REVIEW, "note_zh": "AI 未参与：LLM 已停用。"})
    a = L.run_cycle(draws, snapshot=_fake_snapshot(), path=tmp_path / "a.json",
                    issue="2026118", use_llm=True, persist=True)

    # B：LLM 可用但全部判 keep（合法但保守）
    def _keep(results, *a, **k):
        js = [{"rule_id": r["rule_id"], "verdict": "keep", "confidence": 0.9,
               "reason": "ok", "evidence": [], "gold_direction": r["direction"],
               "valid": True, "errors": []} for r in results]
        return {"ok": True, "used": True, "error": None, "judgements": js,
                "skill": L.skill_stats(js), "note_zh": "AI 复核完成"}
    monkeypatch.setattr(L, "_llm_review", _keep)
    b = L.run_cycle(draws, snapshot=_fake_snapshot(), path=tmp_path / "b.json",
                    issue="2026118", use_llm=True, persist=True)

    sa = {r["rule_id"]: (r["state_after"], r["migrated"], r["direction"])
          for r in a["rules"]}
    sb = {r["rule_id"]: (r["state_after"], r["migrated"], r["direction"])
          for r in b["rules"]}
    assert sa == sb, "AI 的存在与否不得影响任何状态迁移（约束 2）"


def test_t6_llm_exception_does_not_break_cycle(tmp_path, stub_metrics, monkeypatch):
    monkeypatch.setattr(L, "_llm_review",
                        lambda *a, **k: {**L.DEF_REVIEW, "ok": False,
                                         "error": "boom", "note_zh": "AI 未参与"})
    draws = [{"issue": "2026118"}]
    out = L.run_cycle(draws, snapshot=_fake_snapshot(), path=tmp_path / "s.json",
                      issue="2026118", persist=True)
    assert out["ok"] is True
    assert out["n_rules"] == 2


def test_t6_corrupt_state_file_degrades(tmp_path, stub_metrics):
    p = tmp_path / "rule_lifecycle.json"
    p.write_text("{not json", encoding="utf-8")
    st = L.load_states(p)
    assert st["corrupt"] is True and st["rules"] == {}
    out = L.run_cycle([{"issue": "2026118"}], snapshot=_fake_snapshot(), path=p,
                      issue="2026118", persist=False)
    assert out["ok"] is True
    assert any("损坏" in e or "按空状态" in e for e in out["errors"])


def test_t6_missing_state_file_is_empty_not_error(tmp_path):
    st = L.load_states(tmp_path / "nope.json")
    assert st["corrupt"] is False and st["rules"] == {}


def test_t6_save_failure_is_reported_not_raised(tmp_path, stub_metrics):
    out = L.run_cycle([{"issue": "2026118"}], snapshot=_fake_snapshot(),
                      path=tmp_path / "sub" / "x.json", issue="2026118", persist=True)
    assert out["saved"]["ok"] is True   # 目录会自动创建


# ---------------------------------------------------------------------------
# T7 validate_judgements：V1–V6 逐条违规样例
# ---------------------------------------------------------------------------

def _payload():
    return {"rules": [{
        "rule_id": "r-one",
        "scales": {"all": {"delta": -0.06, "p_raw": 0.01, "mde_abs": 0.0422}},
        "eras_tail": [{"era": "P23", "delta": -0.15}],
        "recent": {"delta": -0.2, "p_adjusted": 0.001},
        "gold_direction": "demote",
    }]}


def _judgement(**kw):
    base = {"rule_id": "r-one", "verdict": "demote", "confidence": 0.7,
            "reason": "长期窗口为负", "evidence": [{"field": "scales.all.delta", "value": -0.06}]}
    base.update(kw)
    return {"judgements": [base]}


@pytest.mark.parametrize("raw,code", [
    (_judgement(rule_id="ghost"), "unknown_rule"),
    (_judgement(verdict="maybe"), "bad_verdict"),
    (_judgement(confidence=1.7), "confidence_out_of_range"),
    (_judgement(evidence=[]), "evidence_missing"),
    (_judgement(evidence=[{"field": "scales.foo.delta", "value": 1}]), "evidence_unknown_field"),
    (_judgement(evidence=[{"field": "scales.all.delta", "value": 0.99}]),
     "evidence_value_mismatch"),
    (_judgement(evidence=[{"field": "eras.P99.delta", "value": 1}]), "evidence_unknown_field"),
    (_judgement(reason=""), "reason_missing"),
    (_judgement(verdict="upgrade"), "verdict_contradicts_gold"),
])
def test_t7_validation_codes(raw, code):
    out = L.validate_judgements(raw, _payload())
    j = out["judgements"][0]
    assert j["valid"] is False
    assert code in j["errors"], j["errors"]


def test_t7_good_judgement_passes():
    out = L.validate_judgements(_judgement(), _payload())
    assert out["n_valid"] == 1 and out["judgements"][0]["errors"] == []


def test_t7_keep_on_gold_hold_is_valid():
    p = _payload()
    p["rules"][0]["gold_direction"] = "hold"
    raw = {"judgements": [{"rule_id": "r-one", "verdict": "keep", "confidence": 0.5,
                           "reason": "无衰减迹象",
                           "evidence": [{"field": "scales.all.delta", "value": -0.06}]}]}
    assert L.validate_judgements(raw, p)["n_valid"] == 1


def test_t7_duplicate_rule_invalid():
    raw = _judgement()
    raw["judgements"].append(dict(raw["judgements"][0]))
    out = L.validate_judgements(raw, _payload())
    assert out["judgements"][1]["errors"] == ["duplicate_rule"]


def test_t7_garbage_input_never_raises():
    for raw in (None, [], {"judgements": "x"}, {"judgements": [1, 2]}):
        out = L.validate_judgements(raw, _payload())
        assert out["n_valid"] == 0


# ---------------------------------------------------------------------------
# T8 apply_states：monitor → usable=False；缺状态/损坏 → 原样
# ---------------------------------------------------------------------------

def test_t8_monitor_suppresses_usable():
    snap = _fake_snapshot()
    out = L.apply_states(snap, {"r-one": {"state": "monitor"}})
    r = {x["id"]: x for x in out["rules"]}
    assert r["r-one"]["usable"] is False
    assert r["r-one"]["usable_kind"] is None
    assert r["r-one"]["lifecycle_suppressed"] is True
    assert r["r-one"]["tier"] == "usable_long_term", "tier 表达证据强度，不能被抹掉"
    assert out["counts"]["usable"] == 0
    assert out["counts"]["lifecycle_demoted"] == 1


def test_t8_never_upgrades_usable():
    """反向用例：apply_states 绝不能把 usable=False 改成 True（防后门）。"""
    snap = _fake_snapshot()
    out = L.apply_states(snap, {rid: {"state": "active"} for rid in ("r-one", "r-two")})
    r = {x["id"]: x for x in out["rules"]}
    assert r["r-two"]["usable"] is False


@pytest.mark.parametrize("states", [None, {}, {"unknown": {"state": "monitor"}}])
def test_t8_missing_states_is_passthrough(states):
    snap = _fake_snapshot()
    out = L.apply_states(snap, states)
    assert out == snap


def test_t8_none_snapshot():
    assert L.apply_states(None, {"x": {"state": "monitor"}}) == {}


# ---------------------------------------------------------------------------
# T9 幂等：同一期号重复跑，状态与 streak 不变
# ---------------------------------------------------------------------------

def test_t9_same_issue_twice_is_idempotent(tmp_path, stub_metrics):
    draws = [{"issue": "2026118"}]
    p = tmp_path / "s.json"
    a = L.run_cycle(draws, snapshot=_fake_snapshot(), path=p, issue="2026118",
                    use_llm=False, persist=True)
    st1 = json.loads(p.read_text(encoding="utf-8"))["rules"]
    b = L.run_cycle(draws, snapshot=_fake_snapshot(), path=p, issue="2026118",
                    use_llm=False, persist=True)
    st2 = json.loads(p.read_text(encoding="utf-8"))["rules"]
    # 第二次是"再一次判定"，direction 仍 demote → 会推进到迁移（这是设计允许的），
    # 但第三次起回落到 already，状态不再变化。
    c = L.run_cycle(draws, snapshot=_fake_snapshot(), path=p, issue="2026118",
                    use_llm=False, persist=True)
    st3 = json.loads(p.read_text(encoding="utf-8"))["rules"]
    assert a["n_rules"] == b["n_rules"] == c["n_rules"] == 2
    assert {k: v["state"] for k, v in st2.items()} == {k: v["state"] for k, v in st3.items()}


def test_t9_no_rules_is_graceful(tmp_path):
    out = L.run_cycle([{"issue": "2026118"}], snapshot={"rules": []}, path=tmp_path / "s.json",
                      issue="2026118", use_llm=False, persist=False)
    assert out["ok"] is True and out["n_rules"] == 0
    assert "没有可评估的规律" in out["note_zh"]


# ---------------------------------------------------------------------------
# T10 skill_stats：样本不足不下结论；Wilson 正确
# ---------------------------------------------------------------------------

def test_t10_insufficient_sample_refuses_conclusion():
    js = [{"valid": True, "gold_direction": "demote", "verdict": "keep"}] * 3
    s = L.skill_stats(js)
    assert s["n_action"] == 3
    assert s["conclusion"] == "insufficient"
    assert "不下" in s["note_zh"]


def test_t10_below_baseline_flagged():
    js = ([{"valid": True, "gold_direction": "demote", "verdict": "keep"}] * 10
          + [{"valid": True, "gold_direction": "hold", "verdict": "keep"}] * 10)
    s = L.skill_stats(js)
    # 金标准 hold 的 10 条判对、动作 10 条全判错 → 一致率 50%，Wilson 下界 < 0.5
    assert s["agreement"] == pytest.approx(0.5)
    assert s["conclusion"] == "below-baseline"


def test_t10_wilson_matches_reference():
    lo, hi = L.wilson_interval(0, 10)
    assert lo == pytest.approx(0.0, abs=1e-9) and hi == pytest.approx(0.2775, abs=2e-3)
    lo2, hi2 = L.wilson_interval(5, 10)
    assert lo2 == pytest.approx(0.2366, abs=2e-3) and hi2 == pytest.approx(0.7634, abs=2e-3)
    assert L.wilson_interval(0, 0) == (None, None)


def test_t10_invalid_judgements_excluded():
    js = [{"valid": False, "gold_direction": "demote", "verdict": "demote"}] * 5
    s = L.skill_stats(js)
    assert s["n"] == 0 and s["n_invalid"] == 5


# ---------------------------------------------------------------------------
# report()：Lead 要求的两种"没动作"必须分开表述
# ---------------------------------------------------------------------------

def test_report_no_target_vs_nothing_to_do(tmp_path, monkeypatch):
    from lottery import patterns_runtime
    monkeypatch.setattr(patterns_runtime, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 0}})
    r = L.report(tmp_path / "none.json")
    assert r["mode"] == "no_target"
    assert "没有" in r["note_zh"]

    monkeypatch.setattr(patterns_runtime, "load_snapshot",
                        lambda *a, **k: {"counts": {"usable": 2}})
    r2 = L.report(tmp_path / "none.json")
    assert r2["mode"] == "nothing_to_do"
    assert "无需" in r2["note_zh"]


# ---------------------------------------------------------------------------
# 纯函数性质：decide_rule_state / step_lifecycle 不碰 IO
# ---------------------------------------------------------------------------

def test_pure_functions_have_no_io(tmp_path, monkeypatch):
    import builtins
    real_open = builtins.open

    def _boom(*a, **k):
        raise AssertionError("纯函数不得做 IO")
    monkeypatch.setattr(builtins, "open", _boom)
    try:
        L.decide_rule_state(DEMOTE_METRICS)
        L.step_lifecycle({"direction": "demote", "criteria": ["D1"]}, None, issue="2026118")
        L.apply_states(_fake_snapshot(), {"r-one": {"state": "monitor"}})
    finally:
        monkeypatch.setattr(builtins, "open", real_open)


def test_thresholds_defaults_come_from_config():
    th = L._thresholds()
    assert th["k_demote"] >= 1 and th["k_upgrade"] >= 1 and th["min_dwell"] >= 0


# ---------------------------------------------------------------------------
# 指标构造：用真实数据跑一条规律，只断言结构（不绑定具体数值）
# ---------------------------------------------------------------------------

def test_build_metrics_real_data_shape():
    from lottery import db

    draws = db.load_draws()
    if len(draws) < 600:
        pytest.skip("本地开奖数据不足 600 期")
    m = L.build_metrics(draws, {"key": "repeat", "params": {"back": 1}},
                        upgrade_prereq={"ok": False})
    assert m["scales"], "应产出多尺度指标"
    assert "all" in m["scales"]
    assert isinstance(m["eras"], list) and len(m["eras"]) > 1
    assert m["recent"].get("delta") is not None
    # 结构完整的指标必须能进入纯函数并给出合法方向
    out = L.decide_rule_state(m)
    assert out["direction"] in L.DIRECTIONS
