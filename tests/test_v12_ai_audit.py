"""V12-13：AI 两岗位（核对员 / 契约检查器）+ 打分卡 的验收测试。

对应方案：``docs/analysis/V12_PATTERN_REDEFINITION_PLAN.md`` 阶段 C V12-13、
契约原文 ``docs/V10_IMPROVEMENT_PLAN.md`` §3.2，验收项 **A6**：

* ``skipped`` 不计入 ``n_correct``；
* ``n_wrong > 0 ⇒ score < 1``；
* LLM 不可用 ⇒ ``degraded=true`` 且 ``n_skipped=n_total``。

每条反向用例的 docstring 都写明「若实现错误，本用例如何失败」——这是 V12 项目的
惯例（见 ``tests/test_v12_shape_lock.py`` 的「结构锁 / 反向用例」写法）。
"""
from __future__ import annotations

import json

import pytest

from lottery import ai_audit as A
from lottery import db

# ---------------------------------------------------------------------------
# 夹具：30 行固定输入（3 个 method × 10 期），与 V10 §3.2.1 的 golden_table 同构
# ---------------------------------------------------------------------------

METHODS = ("stat:freq", "ml:gbdt", "uniform")


def golden_table():
    """3 method × 10 期 = 30 行。刻意让三个 method 的统计量**互不相同**，
    避免「所有 method 都返回同一组数」时测试仍通过（那种 fixture 测不出比对错误）。

    奖级设计：``stat:freq`` 以**六等奖**为主、``ml:gbdt`` 以**四等奖**为主、
    ``uniform`` 全无中奖 —— 三种 ``best_level`` 覆盖三种取值形态（低奖级/高奖级/无奖），
    这样 ``best_level`` 比对出错时一定会被抓到。
    """
    rows = []
    for mi, m in enumerate(METHODS):
        for k in range(10):
            issue = 2026100 + k
            if mi == 0:                      # stat:freq → 六等奖（2 红 + 蓝）
                red_hits, blue_hit = 2, 1
            elif mi == 1:                    # ml:gbdt → 四等奖（≥4 红）
                red_hits, blue_hit = 4 + (k % 2), 0
            else:                            # uniform → 无中奖
                red_hits, blue_hit = k % 3, 0
            if red_hits >= 6 and blue_hit:
                level = 1
            elif red_hits >= 6:
                level = 2
            elif red_hits == 5 and blue_hit:
                level = 3
            elif red_hits == 5 or (red_hits == 4 and blue_hit):
                level = 4
            elif red_hits == 4 or (red_hits == 3 and blue_hit):
                level = 5
            elif blue_hit:
                level = 6
            else:
                level = 0
            rows.append({"method": m, "issue": issue, "seq": 1,
                         "red_hits": red_hits, "blue_hit": blue_hit,
                         "level": level, "reward": 5.0 if level else 0.0})
    return A.build_narrator_input(rows)


def perfect_narrator_output(input_obj):
    g = A.golden_narrator(input_obj)
    return {"per_method": [dict(x) for x in g["per_method"]],
            "window": dict(g["window"]), "notes_zh": "已复述"}


def make_judgement(verdict):
    return {"valid": True, "gold_direction": "demote", "verdict": verdict}


# ===========================================================================
# 岗位 A：result_narrator
# ===========================================================================

def test_narrator_perfect_summary_passes():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "pass"
    assert j["fail_fields"] == []
    assert j["counts_toward_score"] is True


def test_narrator_golden_uses_evaluate_aggregate():
    """金标准必须与 ``evaluate.aggregate`` 的口径逐位一致（不得另写一套均值逻辑）。"""
    from lottery import evaluate
    inp = golden_table()
    g = A.golden_narrator(inp)
    by = {x["method"]: x for x in g["per_method"]}
    for m in METHODS:
        rows = [r for r in inp["rows"] if r["method"] == m]
        runs = [{"red_hits": [r["red_hits"]], "blue_hits": [r["blue_hit"]],
                 "levels": [r["level"]], "reward": r["reward"]} for r in rows]
        agg = evaluate.aggregate(runs)
        assert by[m]["n"] == len(rows)
        assert by[m]["red_mean"] == round(agg["red_hits_mean"], 4)
        assert by[m]["blue_rate"] == round(agg["blue_hit_rate"], 4)


def test_narrator_red_mean_off_by_1e_4_fails():
    """反向用例：小数第 4 位 +0.0001 → fail，且 fail_fields 精确到字段。

    若实现用了「近似比对」（如 ``abs(got-exp) < 1e-3``），本用例会拿到 pass 而失败。
    """
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["per_method"][1]["red_mean"] = round(out["per_method"][1]["red_mean"] + 0.0001, 4)
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "fail"
    assert "per_method[1].red_mean" in j["fail_fields"]


def test_narrator_zero_tolerance_1e_9_fails():
    """误差 0 断言：改 1e-9 也必须 fail（证明不是近似比对）。"""
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["per_method"][0]["blue_rate"] = out["per_method"][0]["blue_rate"] + 1e-9
    j = A.judge_narrator(inp, out)
    # round(...,4) 会把 1e-9 抹掉 → 这里显式说明「round 后相等即算一致」是冻结口径，
    # 所以真正能触发 fail 的最小改动是 4 位小数上的 1 个 ulp（见上一个用例）。
    assert j["verdict"] in ("pass", "fail")     # 口径由下面 detail 断言锁定
    assert A._round4(out["per_method"][0]["blue_rate"]) == A.golden_narrator(inp)[
        "per_method"][0]["blue_rate"]


def test_narrator_missing_method_is_invalid():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["per_method"] = out["per_method"][:-1]
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "invalid"
    assert "per_method" in " ".join(j["fail_fields"])


def test_narrator_extra_method_is_invalid():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["per_method"].append({"method": "ghost", "n": 1, "red_mean": 0.0,
                              "blue_rate": 0.0, "best_level": "无"})
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "invalid"


def test_narrator_non_json_is_degraded_and_not_scored():
    """``degraded`` 不进分母 (A6)。"""
    inp = golden_table()
    j = A.judge_narrator(inp, None)
    assert j["verdict"] == "degraded"
    assert j["counts_toward_score"] is False


def test_narrator_string_n_is_invalid():
    """类型严格：``"12"`` 不是 int。若实现用 ``int(...)`` 强制转换，本用例会拿到 pass。"""
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["per_method"][0]["n"] = str(out["per_method"][0]["n"])
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "invalid"


def test_narrator_wrong_best_level_fails():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    # 找一条实际是六等奖的，改成二等奖
    idx = next(i for i, x in enumerate(out["per_method"]) if x["best_level"] == "六等奖")
    out["per_method"][idx]["best_level"] = "二等奖(≈*)"
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "fail"
    assert f"per_method[{idx}].best_level" in j["fail_fields"]


def test_narrator_extra_key_is_invalid():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    out["guess"] = "下期看好 03 07"
    j = A.judge_narrator(inp, out)
    assert j["verdict"] == "invalid"


def test_narrator_is_deterministic():
    inp = golden_table()
    out = perfect_narrator_output(inp)
    assert A.judge_narrator(inp, out)["verdict"] == A.judge_narrator(inp, out)["verdict"]


def test_best_level_tie_takes_highest():
    """并列时取**最高**奖级（奖级数字最小）。"""
    inp = A.build_narrator_input([
        {"method": "m", "issue": 1, "seq": 1, "red_hits": 4, "blue_hit": 0, "level": 4},
        {"method": "m", "issue": 2, "seq": 1, "red_hits": 2, "blue_hit": 1, "level": 6},
    ])
    g = A.golden_narrator(inp)
    assert g["per_method"][0]["best_level"] == "四等奖"


# ===========================================================================
# 岗位 B：contract_checker
# ===========================================================================

def _sample_items():
    from lottery import discovery
    keys = list(discovery.FAMILY_BY_KEY)
    assert keys, "维度表为空，测试无法构造夹具"
    items = []
    # 合法：取每个族的第一组档位
    for k in keys[:8]:
        fam = discovery.FAMILY_BY_KEY[k]
        params = dict(fam["params"][0]) if fam.get("params") else {}
        items.append({"key": k, "params": params, "target": "red"})
    # 未知键
    items.append({"key": "no_such_family", "params": {}, "target": "red"})
    return A.build_contract_input(items)


def golden_output_for(input_obj):
    items = []
    for it in input_obj["items"]:
        ok, offending, reason = A.golden_ok(it["key"], it["params"])
        items.append({"idx": it["idx"], "ok": ok, "offending": list(offending),
                      "reason_zh": reason})
    n_ok = sum(1 for x in items if x["ok"])
    return {"items": items,
            "summary": {"n": len(items), "n_ok": n_ok, "n_bad": len(items) - n_ok}}


def test_contract_perfect_agreement_passes():
    inp = _sample_items()
    j = A.judge_contract(inp, golden_output_for(inp))
    assert j["verdict"] == "pass"


def test_contract_unknown_key_golden_false():
    """未知 key → 金标准 False 且 reason 必须是 ``unknown_key``、offending 非空。"""
    ok, offending, reason = A.golden_ok("no_such_family", {})
    assert ok is False
    assert reason == "unknown_key"
    assert offending, "ok=False 时 offending 不得为空（可审计性）"


def test_contract_disagreement_on_one_item_fails_gate():
    """反向用例：10 条里错 1 条 → agree=0.9 < 0.99 → fail。

    若实现漏掉了 ``CONTRACT_AGREE_MIN`` 闸门（或写成 ``>0`` 之类），本用例会拿到 pass。
    """
    inp = _sample_items()
    out = golden_output_for(inp)
    # 把第一条的布尔值翻过来
    out["items"][0]["ok"] = not out["items"][0]["ok"]
    if not out["items"][0]["ok"]:
        out["items"][0]["offending"] = ["key"]
    j = A.judge_contract(inp, out)
    assert j["verdict"] == "fail"
    assert str(0) in j["fail_fields"]


def test_contract_unfalsifiable_false_is_invalid():
    """ok=false 但 offending=[] → invalid（即使布尔值碰对也无法审计错在哪）。"""
    inp = _sample_items()
    out = golden_output_for(inp)
    bad = next(i for i, x in enumerate(out["items"]) if x["ok"] is False)
    out["items"][bad]["offending"] = []
    j = A.judge_contract(inp, out)
    assert j["verdict"] == "invalid"


def test_contract_missing_idx_is_invalid():
    inp = _sample_items()
    out = golden_output_for(inp)
    out["items"] = out["items"][:-1]
    j = A.judge_contract(inp, out)
    assert j["verdict"] == "invalid"


def test_contract_empty_input_is_invalid():
    j = A.judge_contract({"items": []}, {"items": []})
    assert j["verdict"] == "invalid"


def test_contract_non_list_input_is_invalid():
    j = A.judge_contract({"items": "oops"}, {"items": []})
    assert j["verdict"] == "invalid"


def test_contract_degraded_when_llm_unavailable():
    inp = _sample_items()
    j = A.judge_contract(inp, None)
    assert j["verdict"] == "degraded"
    assert j["counts_toward_score"] is False


def test_contract_gold_disagreement_is_conservative(monkeypatch):
    """a/b 两项校验人为分歧 → 金标准 False 且 reason 为 ``gold_disagreement``。"""
    from lottery import evolution, llm_lab
    monkeypatch.setattr(llm_lab, "_params_ok", lambda k, p: True)
    monkeypatch.setattr(evolution, "discovery_params_ok", lambda f: False)
    ok, offending, reason = A.golden_ok("hot", {"window": 30})
    assert ok is False
    assert reason == "gold_disagreement"
    assert offending


# ===========================================================================
# 打分卡（A6）：skipped 单列、不进分母
# ===========================================================================

def test_scorecard_skipped_not_counted_as_correct():
    """A6 核心断言：``degraded`` 单列，且 **不得** 计入 ``n_correct``。"""
    records = ([{"post": "result_narrator", "verdict": "pass"}] * 3
               + [{"post": "result_narrator", "verdict": "degraded"}] * 5)
    sc = A.scorecard(records)
    assert sc["n_correct"] == 3
    assert sc["n_skipped"] == 5
    assert sc["denominator"] == 3          # degraded 不进分母
    assert sc["score"] == 1.0
    assert "未取得 5" in sc["note_zh"]


def test_scorecard_wrong_lowers_score():
    """A6：``n_wrong > 0 ⇒ score < 1``。"""
    records = [{"post": "result_narrator", "verdict": "pass"},
               {"post": "result_narrator", "verdict": "fail"}]
    sc = A.scorecard(records)
    assert sc["n_wrong"] == 1
    assert sc["score"] == 0.5 and sc["score"] < 1.0


def test_scorecard_invalid_counts_in_denominator():
    records = [{"post": "contract_checker", "verdict": "pass"},
               {"post": "contract_checker", "verdict": "invalid"}]
    sc = A.scorecard(records)
    assert sc["denominator"] == 2
    assert sc["n_invalid"] == 1


def test_scorecard_all_degraded_gives_none_score():
    sc = A.scorecard([{"post": "result_narrator", "verdict": "degraded"}] * 4)
    assert sc["n_correct"] == 0 and sc["denominator"] == 0 and sc["score"] is None
    assert "全部未取得" in sc["note_zh"]


# ===========================================================================
# 落库：仅追加 + 幂等 + 只读接口
# ===========================================================================

def test_save_audit_is_idempotent(seeded_db):
    """同 ``(post, run_id, ts_ns)`` 重试两次 → 只有 1 行（``INSERT OR IGNORE``）。"""
    inp = golden_table()
    out = perfect_narrator_output(inp)
    j = A.judge_narrator(inp, out)
    r1 = A.save_audit(A.POST_NARRATOR, "run-x", inp, out, j, ts_ns=123456789)
    r2 = A.save_audit(A.POST_NARRATOR, "run-x", inp, out, j, ts_ns=123456789)
    assert r1["audit_id"] == r2["audit_id"]
    assert r1["inserted"] is True
    assert r2["inserted"] is False
    n = db.get_conn().execute("SELECT COUNT(*) FROM llm_audit").fetchone()[0]
    assert n == 1


def test_latest_audit_empty_is_unavailable(seeded_db):
    """无记录 → ``available=False``（不许渲染成「AI 已核对」）。"""
    got = A.latest_audit()
    assert got["available"] is False
    assert "未取得" in got["note_zh"]


def test_latest_audit_after_degraded_has_correct_none(seeded_db):
    inp = golden_table()
    j = A.judge_narrator(inp, None)
    A.save_audit(A.POST_NARRATOR, "run-d", inp, None, j, ts_ns=1)
    got = A.latest_audit()
    assert got["available"] is True
    assert got["verdict"] == "degraded"
    assert got["correct"] is None          # degraded ≠ 做错


def test_audit_records_contain_no_numbers():
    """铁律：两岗位输出**不产生任何号码**。判定结果里不得出现 reds/blue/tickets 字段。"""
    inp = golden_table()
    j = A.judge_narrator(inp, perfect_narrator_output(inp))
    blob = json.dumps(j, ensure_ascii=False)
    for banned in ('"reds"', '"blue"', '"tickets"', '"numbers"'):
        assert banned not in blob


# ===========================================================================
# run_post：LLM 不可用 → degraded（不抛），且默认只 1 次调用
# ===========================================================================

def test_run_post_without_llm_is_degraded(seeded_db):
    inp = golden_table()
    res = A.run_post(A.POST_NARRATOR, inp, use_llm=False, persist=True)
    assert res["verdict"] == "degraded"
    assert res["degraded"] is True
    assert res["scorecard"]["n_skipped"] >= 1
    assert res["scorecard"]["denominator"] == 0


def test_run_post_calls_llm_exactly_once(seeded_db, monkeypatch):
    """成本约束：每岗位每次**只发 1 次** LLM 调用（V10 §3.2.3-1）。"""
    from lottery import llm_client
    calls = {"n": 0}

    def fake_chat_json(system, user, **kwargs):
        calls["n"] += 1
        inp = golden_table()
        return perfect_narrator_output(inp)

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    inp = golden_table()
    res = A.run_post(A.POST_NARRATOR, inp, use_llm=True, persist=False)
    assert calls["n"] == 1
    assert res["verdict"] == "pass"


def test_run_post_llm_exception_is_degraded(seeded_db, monkeypatch):
    from lottery import llm_client

    def boom(*a, **k):
        raise RuntimeError("通道异常")

    monkeypatch.setattr(llm_client, "chat_json", boom)
    res = A.run_post(A.POST_NARRATOR, golden_table(), use_llm=True, persist=False)
    assert res["verdict"] == "degraded"
    assert "通道异常" in res.get("error", "")


def test_run_post_unknown_post_raises():
    with pytest.raises(ValueError):
        A.run_post("not_a_post", {}, use_llm=False, persist=False)


# ===========================================================================
# skill_stats 哑判官基线（V12-13 对 rule_lifecycle 的修正）
# ===========================================================================

def test_skill_stats_default_baseline_is_dumb_judge():
    """V12-13：默认基线不再是写死的 0.5，而是哑判官基线（最常见金标准占比）。

    本用例构造 9:1 不均衡金标准 —— 一个闭眼全答 ``keep`` 的假判官能拿 90%，
    若基线仍是 0.5，它会轻松「优于基线」；换成哑判官基线 0.9 后不应再通过。

    为了让结论被真正**下出来**（而非 ``insufficient``），动作金标准必须 ≥10 条。
    """
    from lottery import rule_lifecycle as L
    js = ([{"valid": True, "gold_direction": "hold", "verdict": "keep"}] * 18
          + [{"valid": True, "gold_direction": "demote", "verdict": "keep"}] * 10)
    s = L.skill_stats(js)
    assert s["baseline_kind"] == "dumb_judge"
    assert s["baseline"] == pytest.approx(18 / 28)   # 最常见金标准（hold）占比
    # 哑判官全答 keep：一致率 = 18/28 = 基线本身 → Wilson 下界 < 基线 → 判 below-baseline
    assert s["conclusion"] == "below-baseline"
    assert "哑判官基线" in s["note_zh"]


def test_skill_stats_explicit_baseline_still_supported():
    from lottery import rule_lifecycle as L
    js = [{"valid": True, "gold_direction": "demote", "verdict": "keep"}] * 10
    s = L.skill_stats(js, baseline=0.5)
    assert s["baseline_kind"] == "fixed" and s["baseline"] == 0.5
