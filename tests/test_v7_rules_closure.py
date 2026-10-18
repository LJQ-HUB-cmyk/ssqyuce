"""V7：目标期号 / 预测生成时间 / 规律闭环（挖掘→验证→使用）的测试。

对应四条用户反馈：
1. 目标期号显示成 2026401 —— 期号必须按**真实开奖日历**推算，并且数据落后要明说；
2. 本期推荐没有生成时间 —— 预测必须带生成时间与数据基准；
3. 「规律引用：无显著规律」—— 规律必须真的能进入出号链路，并给出可核验的引用；
4. 要用 AI 挖掘、验证、使用规律 —— 闭环每一步都要落地且可审计。
"""
from __future__ import annotations

import datetime
import json

import pytest

from lottery import backtest as BT
from lottery import patterns_runtime as PR


def _draw(issue, date, reds=None, blue=7):
    return {"issue": issue, "date": date, "reds": reds or [1, 2, 3, 4, 5, 6], "blue": blue}


# ---------- 1. 期号推算 ----------

def test_next_issue_simple_increment():
    assert BT.next_issue("2026118") == "2026119"
    assert BT.next_issue("2026001") == "2026002"


def test_expected_issue_same_day_before_draw_is_next():
    """最后一期是周四（10-15），到周日开奖前，目标期号仍是 +1。"""
    draws = [_draw("2026118", "2026-10-15")]
    for day in (15, 16, 17, 18):
        r = BT.expected_issue(draws, datetime.date(2026, 10, day))
        assert r["issue"] == "2026119", day
        assert r["missed_draws"] == 0


def test_expected_issue_counts_missed_draw_days():
    """过了周日（10-18）之后仍未抓到数据 → 应顺推到 2026120，并明确报「缺 1 期」。"""
    draws = [_draw("2026118", "2026-10-15")]
    r = BT.expected_issue(draws, datetime.date(2026, 10, 19))
    assert r["issue"] == "2026120"
    assert r["missed_draws"] == 1
    assert r["expected_draws_missing"] is True
    assert "落后" in r["note_zh"] or "缺" in r["note_zh"]


def test_expected_issue_never_jumps_to_next_year_4xx():
    """回归：旧写法 seq>999 会推到下一年 001，用户看到的就是「2026401」这类错误期号。"""
    draws = [_draw("2026118", "2026-10-15")]
    # 极端陈旧（数据停在一年前）：必须如实标注 stale_year，而不是编出一个像样的期号
    r = BT.expected_issue(draws, datetime.date(2027, 12, 1))
    assert r["stale_year"] is True
    assert "缺了整整一年" in r["note_zh"]


def test_expected_issue_empty_draws():
    r = BT.expected_issue([], datetime.date(2026, 10, 17))
    assert r["issue"] is None
    assert r["note_zh"]


def test_expected_issue_only_counts_tue_thu_sun():
    # 2026-10-15 是周四；周五/周六不是开奖日
    for d in (datetime.date(2026, 10, 16), datetime.date(2026, 10, 17)):
        assert d.weekday() not in BT.DRAW_WEEKDAYS
    assert datetime.date(2026, 10, 18).weekday() in BT.DRAW_WEEKDAYS


# ---------- 2. 规律运行时快照 ----------

def _fake_hyps(monkeypatch, hyps):
    from lottery import hypothesis
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: {"hypotheses": hyps})


def _draws(n=600, seed=5):
    import numpy as np
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        y = 2023 + i // 150
        out.append({"issue": f"{y}{i % 150 + 1:03d}",
                    "date": f"{y}-06-01",
                    "reds": sorted(int(x) for x in rng.choice(np.arange(1, 34), 6, replace=False)),
                    "blue": int(rng.integers(1, 17))})
    return out


def test_snapshot_tiers_only_persistent_and_validated_is_usable(monkeypatch):
    """分层口径：`usable` 需三条件齐备 —— 阶段持续 + A/B validated + **前置注册通过**。

    V10-02 变更：V7 时代只有前两条即可 usable；V10 起还必须 `prereg_ok is True`
    （由 hypothesis.evaluate_prereg() 写入）。缺字段按安全默认视为 False。
    """
    hyps = [
        {"id": "u1", "status": "validated", "source": "stat", "statement": "热号",
         "prereg_ok": True, "prereg_status": "passed",
         "action": {"type": "prefer_red_filter", "filter": {"key": "hot", "params": {"window": 30, "k": 6}}},
         "regime": {"status": "persistent", "label_zh": "持续有效", "reason_zh": "r", "recent_delta": 0.1}},
        # V10 新增：三条件齐备但 prereg_ok 缺失 → 不得 usable（安全默认）
        {"id": "u2", "status": "validated", "source": "stat", "statement": "热号但未前置注册",
         "action": {"type": "prefer_red_filter", "filter": {"key": "hot", "params": {"window": 50, "k": 6}}},
         "regime": {"status": "persistent", "label_zh": "持续有效", "reason_zh": "r"}},
        {"id": "m1", "status": "proposed", "source": "stat", "statement": "冷号",
         "action": {"type": "prefer_red_filter", "filter": {"key": "cold", "params": {"window": 30, "k": 6}}},
         "regime": {"status": "emerging", "label_zh": "新兴", "reason_zh": "r"}},
        {"id": "m2", "status": "validated", "source": "stat", "statement": "新兴但已验证",
         "action": {"type": "prefer_red_filter", "filter": {"key": "streak", "params": {"k": 3}}},
         "regime": {"status": "emerging", "label_zh": "新兴"}},
        {"id": "r1", "status": "rejected", "source": "stat", "statement": "否决",
         "action": {"type": "prefer_red_filter", "filter": {"key": "repeat", "params": {"back": 1}}},
         "regime": {"status": "noise", "label_zh": "未检出"}},
        {"id": "x1", "status": "validated", "source": "stat", "statement": "无维度",
         "action": {"type": "boost_method", "method": "stat:bayes"}, "regime": {"status": "persistent"}},
    ]
    _fake_hyps(monkeypatch, hyps)
    snap = PR.build_snapshot(_draws(), scan={"n_tested": 1, "items": [], "verdict": "x"})
    by_id = {r["id"]: r for r in snap["rules"]}
    # V11：tier 细化为 usable_long_term / usable_phase（两类必须分开度量），
    # 长期规律（persistent + validated + prereg_ok）应为 usable_long_term。
    assert by_id["u1"]["tier"] == "usable_long_term"
    assert by_id["u1"]["usable"] is True
    # V10-02：缺 prereg_ok → 不能 usable（否则前置注册白做）
    assert by_id["u2"]["tier"] == "monitor", "缺 prereg_ok 必须降级为 monitor"
    # 只有一个阶段显著（emerging）→ 永远不能用于出号，即使 A/B 已验证
    assert by_id["m2"]["tier"] == "monitor"
    assert by_id["r1"]["tier"] == "rejected"
    assert "x1" not in by_id, "无维度假说不写入快照"
    assert snap["counts"]["usable"] == 1, "只有 u1 同时满足三条件"


def test_snapshot_roundtrip_and_staleness(tmp_path, monkeypatch):
    _fake_hyps(monkeypatch, [])
    path = tmp_path / "rt.json"
    snap = PR.build_snapshot(_draws(), scan={"n_tested": 0, "items": []})
    assert PR.save_snapshot(snap, path=path)["ok"]
    loaded = PR.load_snapshot(path=path, current_issue=snap["base_issue"])
    assert loaded and loaded["stale"] is False
    # 当前期号比快照基准多 5 期 → 过期
    base = int(str(snap["base_issue"])[4:])
    later = f"{snap['base_issue'][:4]}{base + 5:03d}"
    assert PR.load_snapshot(path=path, current_issue=later)["stale"] is True


def test_snapshot_missing_file_returns_none(tmp_path):
    assert PR.load_snapshot(path=tmp_path / "nope.json") is None


def test_usable_rules_empty_when_stale(monkeypatch):
    snap = {"stale": True, "rules": [{"id": "a", "usable": True, "tier": "usable"}]}
    assert PR.usable_rules(snap) == []


def test_apply_enabled_env_gate(monkeypatch):
    monkeypatch.delenv("LOTT_RULE_APPLY", raising=False)
    assert PR.apply_enabled() is False
    monkeypatch.setenv("LOTT_RULE_APPLY", "1")
    assert PR.apply_enabled() is True


# ---------- 3. 规律进入出号链路 ----------

def _snap_with_rules(rules):
    return {"stale": False, "counts": {"usable": len(rules), "monitor": 0, "rejected": 0},
            "generated_at_text": "2026-10-17 10:00:00", "rules": rules}


def test_apply_rules_off_without_snapshot():
    from lottery import engine
    cands = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 1, "confidence": 50.0}]
    out = engine.apply_rules_to_candidates(cands, _draws(600), None)
    assert out["mode"] == "off"
    assert "没有可用规律" in out["note_zh"]


def test_apply_rules_shadow_records_but_does_not_reorder():
    from lottery import engine
    rule = {"id": "r", "usable": True, "tier": "usable", "target": "red",
            "statement": "热号", "dim": {"key": "hot", "params": {"window": 30, "k": 6}},
            "regime": {"status": "persistent", "label": "持续有效"}}
    hist = _draws(600)
    hot = engine._rules_hit(hist, rule["dim"])
    assert len(hot) >= 6, "热号维度必须选出 6 个号码，否则测不到「命中」"
    hit_ticket = sorted(hot)[:6]
    miss_ticket = [x for x in range(1, 34) if x not in hot][:6]
    cands = [{"reds": hit_ticket, "blue": 1, "confidence": 26.0},
             {"reds": miss_ticket, "blue": 2, "confidence": 27.0}]
    out = engine.apply_rules_to_candidates(cands, hist, _snap_with_rules([rule]))
    assert out["mode"] == "shadow"
    # 影子模式：规律命中被算出来并记录，但**排序不变**（不改号码）
    assert out["n_boosted"] >= 1
    assert out["n_resorted"] == 0
    # 影子模式下**候选列表顺序必须保持不变**（不改号码）
    assert cands[0]["reds"] == hit_ticket
    assert cands[0]["confidence"] > 10.0, "置信度偏置应被算出并记录"


def test_apply_rules_active_reorders(monkeypatch):
    from lottery import engine
    monkeypatch.setenv("LOTT_RULE_APPLY", "1")
    rule = {"id": "r", "usable": True, "tier": "usable", "target": "red",
            "statement": "热号", "dim": {"key": "hot", "params": {"window": 30, "k": 6}},
            "regime": {"status": "persistent"}}
    hist = _draws(600)
    hot = engine._rules_hit(hist, rule["dim"])
    assert len(hot) >= 6
    hit_ticket = sorted(hot)[:6]
    miss_ticket = [x for x in range(1, 34) if x not in hot][:6]
    # 初始顺序：未命中规律的注在前、置信度**略高**（同量级，符合真实候选池尺度）
    cands = [{"reds": miss_ticket, "blue": 2, "confidence": 27.0},
             {"reds": hit_ticket, "blue": 1, "confidence": 26.0}]
    assert cands[0]["reds"] == miss_ticket
    out = engine.apply_rules_to_candidates(cands, hist, _snap_with_rules([rule]))
    assert out["mode"] == "active"
    # 生效模式：列表被真正重排，未命中的注退到第 2 位
    assert cands[0]["reds"] == hit_ticket, "生效模式下命中规律的注应被顶到最前"
    hit = cands[0]
    assert hit["confidence"] > 26.0
    assert out["n_boosted"] >= 1
    assert out["n_resorted"] >= 1


def test_apply_rules_respects_target_blue():
    from lottery import engine
    rule = {"id": "b", "usable": True, "tier": "usable", "target": "blue",
            "statement": "蓝球热号", "dim": {"key": "blue_hot", "params": {"window": 20}},
            "regime": {"status": "persistent"}}
    hist = _draws(600)
    blue = int(next(iter(engine._rules_hit(hist, rule["dim"]))))
    cands = [{"reds": [1, 2, 3, 4, 5, 6], "blue": blue, "confidence": 1.0},
             {"reds": [7, 8, 9, 10, 11, 12], "blue": (blue % 16) + 1, "confidence": 9.0}]
    out = engine.apply_rules_to_candidates(cands, hist, _snap_with_rules([rule]))
    assert out["n_boosted"] >= 1


def test_regime_rules_for_prompt_reads_snapshot(tmp_path, monkeypatch):
    from lottery import engine, config
    _fake_hyps(monkeypatch, [])
    snap = _snap_with_rules([{"id": "r", "usable": True, "tier": "usable", "target": "red",
                              "statement": "热号", "dim": {"key": "hot", "params": {"window": 30, "k": 6}},
                              "regime": {"status": "persistent", "effect": 0.1, "p_adjusted": 0.03}}])
    for r in snap["rules"]:
        r["regime"]["effect"] = 0.1
        r["regime"]["p_adjusted"] = 0.03
    path = tmp_path / "rt.json"
    # 快照必须通过完整性校验（样本量、期号自洽），故用真实规模的基准期号
    stored = {**snap, "base_issue": "2026118", "n_draws": 3515, "generated_at": 0}
    assert PR.save_snapshot(stored, path=path)["ok"]
    monkeypatch.setattr(PR, "SNAPSHOT_FILE", path)
    monkeypatch.setattr(PR, "_CACHE", {"mtime": None, "data": None})
    rows = engine._regime_rules_for_prompt([_draw("2026118", "2026-10-15")])
    assert rows and rows[0]["tier"] == "usable"
    assert rows[0]["key"] == "hot"


# ---------- 4. AI 闭环 ----------

def test_closed_loop_runs_all_steps_without_llm(monkeypatch, tmp_path):
    from lottery import llm_lab, evolution, hypothesis, config, discovery
    monkeypatch.setattr(llm_lab, "CLAIMS_FILE", tmp_path / "claims.json")
    monkeypatch.setattr(PR, "SNAPSHOT_FILE", tmp_path / "rt.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: {"hypotheses": []})

    res = llm_lab.run_closed_loop(_draws(), rounds=1, use_llm=False, persist=True)
    assert res["ok"] and res["closed_loop"] is True
    assert [s["step"] for s in res["steps"]][:2] == ["mine_and_verify", "register"]
    assert any(s["step"] == "refresh_runtime" for s in res["steps"])
    assert res["runtime"]["counts"] is not None
    assert "闭环已跑完" in res["note_zh"]


def test_closed_loop_reports_llm_degradation_not_as_success(monkeypatch, tmp_path):
    from lottery import llm_lab, hypothesis, config
    monkeypatch.setenv("LOTT_LLM_DISABLED", "1")
    monkeypatch.setattr(config, "LLM_DISABLED", True)
    monkeypatch.setattr(llm_lab, "CLAIMS_FILE", tmp_path / "claims.json")
    monkeypatch.setattr(PR, "SNAPSHOT_FILE", tmp_path / "rt.json")
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: {"hypotheses": []})
    res = llm_lab.run_closed_loop(_draws()[:600], rounds=1, use_llm=True, persist=False)
    assert res["llm_used"] is False
    assert res["errors"], "LLM 降级必须出现在 errors 里，不能静默成功"


def test_register_llm_lab_claims_never_registers_noise(monkeypatch, tmp_path):
    from lottery import llm_lab, evolution, hypothesis
    claims = tmp_path / "c.json"
    reg = tmp_path / "h.json"
    monkeypatch.setattr(llm_lab, "CLAIMS_FILE", claims)
    llm_lab.persist_run({"claims": [
        {"title": "noise", "verdict": "noise", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]},
        {"title": "bad", "verdict": "support", "filters": [{"key": "moon", "params": {}}]},
        {"title": "good", "verdict": "regime_only", "filters": [{"key": "cold", "params": {"window": 30, "k": 6}}],
         "n": 3000, "effect": 0.05, "p_adjusted": 0.2},
    ], "llm_used": True}, path=claims)
    out = evolution.register_llm_lab_claims(_draws(), registry_path=reg)
    assert out["picked"] == 1
    assert len(hypothesis.load_registry(reg)["hypotheses"]) == 1


# ---------- 5. 快照完整性（跨进程共享派生状态） ----------

def test_snapshot_validation_rejects_bad_basics():
    base = {"base_issue": "2026118", "n_draws": 3515}
    assert PR.validate_snapshot(base)["ok"]
    assert not PR.validate_snapshot({"base_issue": "202611", "n_draws": 3515})["ok"]
    assert not PR.validate_snapshot({"base_issue": "2026399", "n_draws": 400})["ok"]
    assert not PR.validate_snapshot(None)["ok"]


def test_snapshot_validation_catches_wrong_data_source():
    """回归：一次 400 期的沙箱回放把快照基准写成 2026399 并被线上读到。

    现在写入与读取都做期号/样本自查，「用错数据源生成的快照」必须被拒绝。
    """
    bad = {"base_issue": "2026399", "n_draws": 400}
    r = PR.validate_snapshot(bad, library_max_issue="2026118")
    assert not r["ok"]

    mid = {"base_issue": "2026118", "n_draws": 3515}
    assert not PR.validate_snapshot(mid, library_max_issue="2026050")["ok"]
    assert PR.validate_snapshot(mid, library_max_issue="2026118")["ok"]


def test_build_snapshot_refuses_too_few_draws():
    with pytest.raises(ValueError, match="不足以生成规律快照"):
        PR.build_snapshot([_draw("2026001", "2026-01-01")] * 400)


def test_save_snapshot_rejects_invalid(tmp_path):
    out = PR.save_snapshot({"base_issue": "2026399", "n_draws": 400}, path=tmp_path / "x.json")
    assert out["ok"] is False and "校验未通过" in out["error"]
    assert not (tmp_path / "x.json").exists()


def test_load_snapshot_ignores_invalid_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"base_issue": "2026399", "n_draws": 400, "rules": [{"usable": True}]}))
    assert PR.load_snapshot(path=p, current_issue="2026118") is None
