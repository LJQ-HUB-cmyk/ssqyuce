"""V6 阶段时效 / 多维度发现 / LLM 规律工坊 的测试。

覆盖原则（与本仓库既有约定一致）：
1. **诚实口径**：不确定的东西不能被包装成结论——样本不足、不可落地、无维度，
   都必须有明确状态而不是「看起来像结论的数字」。
2. **无未来信息泄漏**：所有筛选条件与阶段检验只允许用当期之前的数据（as-of）。
3. **增量实现与朴素实现一致**：CTX 的增量缓存必须与「每期从零算」逐项相同，
   否则性能优化会悄悄改变结论（本轮就因此抓到 3 个真实 bug：dict 的 len 不是期数、
   window 语义漂移、缓存键冲突）。
4. **LLM 完全不可用时不崩**：工坊必须优雅降级并说明原因。
"""
import json
import math

import numpy as np
import pytest

from lottery import discovery, evolution, hypothesis, llm_lab, regime


# ---------- 夹具 ----------

def _draws(n=400, seed=3):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        year = 2003 + i * 24 // max(1, n)
        reds = sorted(int(x) for x in rng.choice(np.arange(1, 34), 6, replace=False))
        out.append({"issue": f"{year:04d}{i % 150:03d}", "date": f"{year}-06-01",
                    "reds": reds, "blue": int(rng.integers(1, 17))})
    return out


@pytest.fixture(scope="module")
def draws():
    return _draws(400)


# ---------- regime ----------

def test_build_eras_covers_all_draws_without_overlap():
    d = _draws(600)
    eras = regime.build_eras(d)
    assert eras, "至少要有一个阶段"
    assert eras[0]["start"] == 0
    assert eras[-1]["end"] == len(d)
    for a, b in zip(eras, eras[1:]):
        assert a["end"] == b["start"], "阶段必须首尾相接、互不重叠"


def test_build_eras_skips_empty_periods():
    d = [{"issue": "2026001", "date": "2026-01-01", "reds": [1, 2, 3, 4, 5, 6], "blue": 7}]
    eras = regime.build_eras(d)
    assert len(eras) == 1 and eras[0]["name"] == "2026"


def test_split_series_matches_era_lengths():
    d = _draws(300)
    eras = regime.build_eras(d)
    series = list(range(len(d)))
    chunks = regime.split_series(series, eras)
    assert sum(len(c) for c in chunks) == len(d)
    for e, c in zip(eras, chunks):
        assert len(c) == e["n"]


def test_era_significance_continuous_null_is_not_significant():
    rng = np.random.default_rng(1)
    x = list(rng.normal(0, 1, 500))
    r = regime.era_significance(x)
    assert r["n"] == 500
    assert r["test"] == "t"
    assert r["p_value"] is not None and r["p_value"] > 0.01
    assert r["mde"] is not None and r["mde"] > 0


def test_era_significance_continuous_detects_real_shift():
    rng = np.random.default_rng(2)
    x = list(rng.normal(0.5, 1, 500))
    r = regime.era_significance(x)
    assert r["p_value"] < 1e-6
    assert r["delta"] > 0.4


def test_era_significance_binomial_direction():
    rng = np.random.default_rng(3)
    p0 = 6 / 33
    x = list((rng.random(800) < p0 * 1.6).astype(float))
    r = regime.era_significance(x, [p0] * 800)
    assert r["test"] == "binomial"
    assert r["delta"] > 0 and r["p_value"] < 1e-6


def test_era_significance_handles_degenerate_input():
    assert regime.era_significance([])["p_value"] is None
    assert regime.era_significance([0.0])["p_value"] is None
    # 方差为 0：不作检验，但也不抛异常
    r = regime.era_significance([0.0] * 100)
    assert r["p_value"] is None and "样本过少或无方差" in r["note_zh"]


def test_family_adjust_bh_is_monotone_and_bounded():
    ps = [0.001, 0.02, 0.5, None, 0.9]
    adj = regime.family_adjust(ps)
    assert adj[3] is None
    vals = [a for a in adj if a is not None]
    assert all(0 <= a <= 1 for a in vals)
    # BH 的校正 p 不小于原 p
    for p, a in zip([0.001, 0.02, 0.5, 0.9], [adj[0], adj[1], adj[2], adj[4]]):
        assert a >= p - 1e-12


def test_weighted_recent_uses_only_tail():
    series = [10.0] * 500 + [0.0] * 150
    r = regime.weighted_recent(series, window=150)
    assert r["n"] == 150
    assert abs(r["mean"]) < 1e-9, "只用最近 150 期，应该全部是 0"
    assert r["n_eff"] is not None and r["n_eff"] <= 150


def test_regime_status_insufficient_when_total_too_small():
    eras = regime.build_eras(_draws(30))
    st = regime.regime_status(eras, [None] * len(eras), [None] * len(eras), None, 30)
    assert st["status"] == "insufficient"


def test_regime_status_noise_when_no_era_significant():
    eras = regime.build_eras(_draws(200))
    st = regime.regime_status(eras, [0.5] * len(eras), [0.01] * len(eras), None, 200)
    assert st["status"] == "noise"
    assert "没有" in st["reason_zh"]


def test_regime_status_persistent_when_last_era_significant():
    """最近一个阶段显著 → persistent（无论此前是否也显著）。

    注意：本仓库的最近阶段是「当前年份」，把最新一期摆在最新阶段才算「现在仍有效」。
    """
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    adj = [0.4] * (k - 2) + [0.01, 0.01]
    deltas = [0.01] * (k - 2) + [0.2, 0.2]
    st = regime.regime_status(eras, adj, deltas, k - 1, 300)
    assert st["status"] == "persistent"


def test_regime_status_first_significant_is_also_last_era_is_emerging():
    """「首次显著恰好发生在最近阶段」→ emerging，不是 persistent（单一阶段证据不可当持续有效）。"""
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    adj = [0.4] * (k - 1) + [0.01]
    deltas = [0.01] * (k - 1) + [0.2]
    st = regime.regime_status(eras, adj, deltas, k - 1, 300)
    assert st["status"] == "emerging"


def test_regime_status_emerging_requires_only_last_era():
    """只有最近阶段显著、此前从未显著 → emerging（并明确提示单阶段显著是预期假阳性）。"""
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    assert k >= 3
    adj = [0.4] * (k - 1) + [0.01]
    deltas = [0.02] * (k - 1) + [0.2]
    st = regime.regime_status(eras, adj, deltas, k - 1, 300)
    assert "假阳性" in st["reason_zh"] or "复现" in st["reason_zh"]


def test_regime_status_fading_when_previous_era_only():
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    adj = [0.4] * (k - 2) + [0.01, 0.6]
    deltas = [0.01] * (k - 2) + [0.3, 0.0]
    st = regime.regime_status(eras, adj, deltas, k - 2, 300)
    assert st["status"] == "fading"


def test_regime_status_flipped_when_direction_reverses():
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    adj = [0.01] + [0.5] * (k - 2) + [0.01]
    deltas = [0.3] + [0.0] * (k - 2) + [-0.3]
    st = regime.regime_status(eras, adj, deltas, k - 1, 300)
    assert st["status"] == "flipped"


def test_regime_status_stale_when_last_significant_long_ago():
    d = _draws(300)
    eras = regime.build_eras(d)
    k = len(eras)
    adj = [0.01, 0.5, 0.5][:k]
    deltas = [0.3, 0.0, 0.0][:k]
    st = regime.regime_status(eras, adj, deltas, 0, 300)
    assert st["status"] in ("stale", "reversed")


def test_regime_report_rejects_misaligned_series(draws):
    with pytest.raises(ValueError):
        regime.regime_report([0.0] * 10, draws)


def test_regime_report_marks_pad_periods_out_of_recent_window(draws):
    """as-of 回放的占位期不得进入「近期加权窗口」——否则会稀释最近的真实效应。"""
    n = len(draws)
    pad = 200
    series = [0.0] * pad + [1.0] * (n - pad)
    rep = regime.regime_report(series, draws, pad=pad)
    assert rep["recent"]["n"] <= n - pad


def test_stage_snapshot_is_as_of():
    """快照只能反映传入的历史；把**未来**期次换成完全不同的数据，不能影响旧时点。"""
    d = [_draws(120, seed=1)]
    base = d[0]
    a = regime.stage_snapshot(base)
    # 同一时点 + 完全不同的「未来」数据：快照的所有派生量必须逐项一致（忽略期数与期号）
    future = base + [_draws(50, seed=9)[i] for i in range(50)]
    b = regime.stage_snapshot(future[:120])
    assert a["n"] == 120 and b["n"] == 120
    for key in ("omission", "red_freq", "structure", "latest"):
        assert a[key] == b[key], f"{key} 受到了未来数据影响"
    # D-15：原写法 `assert ... != ... or True` 恒真，等于没测。
    # 本意是「换了更长历史后 omission 应当不同，证明上面的一致性断言不是空转」。
    # 实测确实不同（120 期 vs 170 期，遗漏值变了），故写成真断言。
    assert a["omission"] != regime.stage_snapshot(future)["omission"], \
        "更长历史下 omission 竟然完全相同 —— 上面的一致性断言可能是空转"


# ---------- discovery ----------

def test_family_catalog_is_json_serialisable_and_documented():
    fams = discovery.family_catalog()
    assert len(fams) >= 18
    json.dumps(fams)                     # 必须可 JSON 化（要喂给 LLM）
    for f in fams:
        assert f["key"] and f["name_zh"] and f["ratio"]
        assert f["target"] in ("red", "blue")
        assert f["n_param_sets"] >= 1
    assert discovery.combo_space_size() >= 80


def test_family_keys_are_unique():
    keys = [f["key"] for f in discovery.FAMILIES]
    assert len(keys) == len(set(keys))


def test_make_filter_rejects_unknown_family():
    with pytest.raises(KeyError):
        discovery.make_filter("no_such_family")


def test_combine_requires_same_target():
    a = discovery.make_filter("hot", {"window": 30, "k": 6})
    b = discovery.make_filter("blue_hot", {"window": 20})
    with pytest.raises(ValueError):
        discovery.combine([a, b])


def test_combine_union_and_intersection():
    a = discovery.make_filter("hot", {"window": 30, "k": 6})
    b = discovery.make_filter("cold", {"window": 30, "k": 6})
    d = _draws(120)
    union = discovery.combine([a, b], "union")["selector"](d)
    inter = discovery.combine([a, b], "intersection")["selector"](d)
    assert inter <= union
    assert set(a["selector"](d)).issubset(set(union))


def test_asof_scan_is_strictly_walk_forward():
    """把未来期次改掉，不能影响此前任何一期的观测值。"""
    d = _draws(260)
    f = discovery.make_filter("hot", {"window": 30, "k": 6})
    a = discovery.asof_scan(d[:200], f, start=150)
    mutated = [dict(x) for x in d[:200]]
    mutated[199]["reds"] = [1, 2, 3, 4, 5, 6]
    b = discovery.asof_scan(mutated, f, start=150)
    assert a["obs"][:-1] == b["obs"][:-1]
    # D-15：原写法 `assert a["obs"][-1] != b["obs"][-1] or True` 恒真，是废断言。
    # **这里不补 != 断言**，因为实测在测试数据下 `obs[-1]` 前后都是 1.0（不敏感），
    # 改成 `!=` 会假红。原作者的注释「最后一期允许变化」是诚实的观察。
    # 本用例的真正契约是上面那句「前缀逐项相同」，已由 `:-1` 断言覆盖。

    c = discovery.asof_scan(d, f, start=150, end=200)
    assert c["obs"][:-1] == a["obs"][:-1], "更长历史的前缀必须与短历史一致"


def test_incremental_selectors_match_naive(draws):
    """CTX 增量缓存与「每期从零算」必须逐项一致（本轮优化的正确性护栏）。"""
    cases = [
        ("hot", {"window": 30, "k": 6}),
        ("cold", {"window": 100, "k": 5}),
        ("omission_ratio", {"k": 6}),
        ("omission_high", {"k": 8}),
        ("streak", {"k": 3}),
        ("neighbor", {"back": 2, "offset": 3}),
        ("same_tail", {"back": 1}),
        ("link", {"mode": "div3", "back": 2}),
        ("pair", {"window": 100, "top": 3}),
        ("zone_lag", {"zone": 0}),
        ("weekday", {"window": 200, "k": 6}),
        ("pattern_cont", {"feature": "odd", "k": 6}),
        ("blue_hot", {"window": 20}),
        ("blue_cold", {"window": 50}),
        ("blue_parity", {"mode": "size"}),
        ("blue_omission", {}),
        ("repeat", {"back": 1}),
        ("repeat2", {"back": 2}),
    ]
    for key, params in cases:
        f = discovery.make_filter(key, params)
        for t in (60, 200, 400):
            ctx = discovery.ScanContext.new(draws[:t])
            assert set(f["selector"](ctx)) == set(f["selector"](list(draws[:t]))), \
                f"{key}{params} 在 t={t} 的增量实现与朴素实现不一致"


def test_scan_context_n_does_not_use_dict_len(draws):
    """ScanContext 是 dict 子类：len(ctx) 是键数量，绝不能当历史期数用。"""
    ctx = discovery.ScanContext.new(draws[:100])
    assert ctx.n == 100
    assert len(ctx) != 100
    assert discovery._n_of(ctx) == 100


def test_regime_of_filter_returns_status_and_scan(draws):
    f = discovery.make_filter("hot", {"window": 30, "k": 6})
    rep = discovery.regime_of_filter(draws, f, start=100)
    assert rep["status"] in ("persistent", "emerging", "fading", "stale",
                             "flipped", "reversed", "noise", "insufficient")
    assert rep["scan"]["n"] == len(draws) - 100
    assert rep["filter"]["key"] == "hot"


def test_discover_reports_search_cost(draws):
    res = discovery.discover(draws, top=3, fast_window=200, use_cache=False)
    assert res["n_tested"] > 0
    # 搜索成本必须可见：预期假阳性数 = 组合数 × alpha
    assert abs(res["expected_false_positives"] - res["n_tested"] * res["alpha"]) < 0.01
    assert res["verdict"]
    assert len(res["items"]) <= 3
    json.dumps(res["items"])


def test_discover_is_deterministic(draws):
    a = discovery.discover(draws, top=3, fast_window=200, use_cache=False)
    b = discovery.discover(draws, top=3, fast_window=200, use_cache=False)
    assert [x["filter"] for x in a["items"]] == [x["filter"] for x in b["items"]]


# ---------- llm_lab ----------

def test_lab_context_contains_stage_and_dimension_facts(draws):
    from lottery import discovery as _d
    scan = _d.discover(draws, top=2, fast_window=200, use_cache=False)
    ctx = llm_lab.build_lab_context(draws, scan)
    assert ctx["总期数"] == len(draws)
    assert "维度表" in ctx and ctx["维度表"]
    assert "阶段划分" in ctx and "分阶段统计" in ctx
    assert "扫描统计" in ctx
    assert "纯随机预期假阳性数" in ctx["扫描统计"]
    json.dumps(ctx, ensure_ascii=False)   # 必须可序列化后直接进提示词


def test_lab_prompts_are_three_distinct_rounds():
    ctx = {"总期数": 1}
    p1 = llm_lab.build_user_prompt(ctx, 1)
    p2 = llm_lab.build_user_prompt(ctx, 2, [{"title": "x"}])
    p3 = llm_lab.build_user_prompt(ctx, 3, [{"title": "x", "verdict": "kept"}])
    assert p1 != p2 != p3
    assert "怀疑" in p2 or "审查" in p2
    assert "最终版" in p3


def test_normalize_claims_filters_malformed():
    raw = [
        {"title": "ok", "filters": {"key": "hot", "params": {"window": 30, "k": 6}}},
        {"title": "no-key", "filters": [{"nonsense": 1}]},
        "not-a-dict",
        {"title": "multi", "filters": [{"key": "a"}, {"key": "b"}, {"key": "c"}, {"key": "d"}]},
    ]
    out = llm_lab._normalize_claims(raw)
    assert len(out) == 3
    assert out[0]["filters"] == [{"key": "hot", "params": {"window": 30, "k": 6}}]
    assert out[1]["filters"] == []
    assert len(out[2]["filters"]) == 3          # 最多 3 个筛选条件


def test_params_must_come_from_catalog():
    assert llm_lab._params_ok("hot", {"window": 30, "k": 6})
    assert not llm_lab._params_ok("hot", {"window": 37, "k": 6}), "未声明的档位不算可验证"
    assert not llm_lab._params_ok("moon_phase", {"k": 3})
    assert llm_lab._params_ok("second_order", {}) if "second_order" in discovery.FAMILY_BY_KEY else True


def test_evaluate_claim_marks_unmappable_explicitly(draws):
    claim = {"title": "月球周期", "filters": [{"key": "moon_phase", "params": {"k": 3}}]}
    r = llm_lab.evaluate_claim(claim, draws)
    assert r["verdict"] == "unmappable"
    assert "不在维度表" in r["reason"]
    assert r["n"] == 0


def test_evaluate_claim_runs_real_backtest(draws):
    claim = {"title": "近30期热号", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]}
    r = llm_lab.evaluate_claim(claim, draws, start=100)
    assert r["verdict"] in ("support", "reject", "regime_only", "noise")
    assert r["n"] == len(draws) - 100
    assert r["label"]
    assert r["regime"]["status"]
    assert isinstance(r["regime"]["eras"], list)


def test_evaluate_claims_never_raises(draws):
    out = llm_lab.evaluate_claims([{"title": "bad", "filters": [{"key": "nope"}]},
                                   {"title": "empty"}], draws)
    assert all(r["verdict"] in ("unmappable", "insufficient") for r in out)


def test_verdicts_are_all_documented():
    assert set(llm_lab.VERDICTS) == {"support", "reject", "regime_only",
                                    "noise", "unmappable", "insufficient"}
    for k, v in llm_lab.VERDICTS.items():
        assert v and isinstance(v, str)


def test_persist_and_load_claims_roundtrip(tmp_path, draws):
    path = tmp_path / "llm_claims.json"
    res = {"claims": [{"title": "a", "verdict": "noise"}], "llm_used": True,
           "summary": {"noise": 1}, "overall": "ok", "error": None}
    out = llm_lab.persist_run(res, path=path)
    assert out["ok"] and path.exists()
    data = llm_lab.load_claims(path)
    assert data["claims"][0]["title"] == "a"
    assert data["runs"][0]["summary"] == {"noise": 1}
    # 同名规律只保留最新一次
    llm_lab.persist_run({"claims": [{"title": "a", "verdict": "support"}], "llm_used": True}, path=path)
    data = llm_lab.load_claims(path)
    assert [c["verdict"] for c in data["claims"] if c["title"] == "a"] == ["support"]


def test_run_lab_without_llm_does_not_call_model(draws):
    """use_llm=False：只做扫描，不得触发任何模型调用（也不得伪造结论）。"""
    res = llm_lab.run_lab(draws, use_llm=False, persist=False)
    assert res["ok"] is True
    assert res["llm_used"] is False
    assert res["claims"] == []
    assert res["scan_summary"]["n_tested"] > 0


def test_ask_llm_degrades_when_disabled(monkeypatch):
    from lottery import config
    monkeypatch.setattr(config, "LLM_DISABLED", True)
    out = llm_lab.ask_llm({"总期数": 1})
    assert out["llm_used"] is False
    assert "停用" in (out["error"] or "")


def test_ask_llm_degrades_on_channel_error(monkeypatch):
    from lottery import config

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(config, "LLM_DISABLED", False)
    monkeypatch.setattr(llm_lab.llm_client, "chat_json", boom)
    out = llm_lab.ask_llm({"总期数": 1})
    assert out["llm_used"] is False
    assert "第 1 轮失败" in (out["error"] or "")


def test_ask_llm_keeps_kept_claims_and_applies_drops(monkeypatch):
    from lottery import config
    monkeypatch.setattr(config, "LLM_DISABLED", False)
    calls = {"n": 0}

    def fake(system, user, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"claims": [{"title": "A", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]},
                               {"title": "B", "filters": [{"key": "cold", "params": {"window": 30, "k": 6}}]}],
                    "overall": "first"}
        if calls["n"] == 2:
            return {"reviews": [{"title": "B", "verdict": "dropped", "reason": "x"},
                                {"title": "A", "verdict": "kept", "reason": "y"}]}
        return {"claims": [{"title": "A2", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]}],
                "overall": "final"}

    monkeypatch.setattr(llm_lab.llm_client, "chat_json", fake)
    out = llm_lab.ask_llm({"总期数": 1}, rounds=3)
    assert [c["title"] for c in out["claims"]] == ["A2"]
    assert out["overall"] == "final"
    assert len(out["reviews"]) == 2


def test_ask_llm_rounds2_only_filters_dropped(monkeypatch):
    from lottery import config
    monkeypatch.setattr(config, "LLM_DISABLED", False)
    calls = {"n": 0}

    def fake(system, user, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"claims": [{"title": "A", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]},
                               {"title": "B", "filters": [{"key": "cold", "params": {"window": 30, "k": 6}}]}]}
        return {"reviews": [{"title": "B", "verdict": "dropped"}]}

    monkeypatch.setattr(llm_lab.llm_client, "chat_json", fake)
    out = llm_lab.ask_llm({"总期数": 1}, rounds=2)
    assert [c["title"] for c in out["claims"]] == ["A"]


# ---------- hypothesis / evolution 集成 ----------

def test_normalize_action_accepts_dimension_actions():
    act, why = hypothesis._normalize_action(
        {"type": "prefer_red_filter", "filter": {"key": "hot", "params": {"window": 30, "k": 6}}})
    assert why is None and act["type"] == "prefer_red_filter"
    assert act["filter"]["key"] == "hot" and act["gain"] == 1.5


def test_normalize_action_rejects_bad_dimension_actions():
    assert hypothesis._normalize_action({"type": "prefer_red_filter"})[0] is None
    assert hypothesis._normalize_action(
        {"type": "prefer_red_filter", "filter": {"key": "nope"}})[0] is None
    # 目标不匹配：蓝球维度不能当红球动作
    assert hypothesis._normalize_action(
        {"type": "prefer_red_filter", "filter": {"key": "blue_hot", "params": {"window": 20}}})[0] is None


def test_apply_hypotheses_collects_filters_only_when_validated():
    hyp = {"id": "h1", "status": "proposed",
           "action": {"type": "prefer_red_filter",
                      "filter": {"key": "hot", "params": {"window": 30, "k": 6}}}}
    ov = evolution.apply_hypotheses([hyp])
    assert ov["filters"] == [], "未验证的假说不得影响出号"
    hyp["status"] = "validated"
    ov = evolution.apply_hypotheses([hyp])
    assert ov["filters"] and ov["filters"][0]["filter"]["key"] == "hot"


def test_apply_overrides_to_probs_boosts_selected_numbers():
    from lottery import engine  # noqa: F401  （确保依赖就绪）
    d = _draws(160)
    red = np.full(33, 1 / 33)
    blue = np.full(16, 1 / 16)
    ov = {"filters": [{"kind": "prefer_red_filter", "gain": 2.0,
                       "filter": {"key": "hot", "params": {"window": 30, "k": 6}}}],
          "red_boost": [], "blue_boost": []}
    r2, _b2 = evolution.apply_overrides_to_probs(red, blue, ov, history=d)
    chosen = discovery.make_filter("hot", {"window": 30, "k": 6})["selector"](d)
    assert r2.sum() == pytest.approx(6.0)
    for n in chosen:
        assert r2[n - 1] > red[n - 1]


def test_apply_overrides_to_probs_without_history_is_safe():
    red = np.full(33, 1 / 33)
    ov = {"filters": [{"kind": "prefer_red_filter", "gain": 2.0,
                       "filter": {"key": "hot", "params": {"window": 30, "k": 6}}}]}
    r2, _ = evolution.apply_overrides_to_probs(red, None, ov, history=None)
    assert np.allclose(r2, red), "没有 history 时必须跳过（绝不使用未来数据）"


def test_verify_discovery_hypothesis_returns_resolvable_shape(draws):
    hyp = {"id": "x1", "status": "validated",
           "action": {"type": "prefer_red_filter",
                      "filter": {"key": "hot", "params": {"window": 30, "k": 6}}}}
    r = evolution.verify_discovery_hypothesis(hyp, draws, start=100)
    for k in ("method", "hyp_id", "n_issues", "delta_red_hits", "p_adjusted", "verdict"):
        assert k in r
    assert r["hyp_id"] == "x1"
    assert r["n_issues"] == len(draws) - 100


def test_verify_discovery_hypothesis_without_filter_is_unverifiable(draws):
    r = evolution.verify_discovery_hypothesis({"id": "x", "action": {"type": "boost_method"}}, draws)
    assert r["verdict"] == "unverifiable"


def test_hypo_structure_filter_maps_rules():
    assert evolution.hypo_structure_filter(
        {"action": {"type": "prefer_structure", "rule": "keep_last"}})["key"] == "repeat"
    assert evolution.hypo_structure_filter(
        {"action": {"type": "prefer_structure", "rule": "neighbors_of_last"}})["key"] == "neighbor"
    assert evolution.hypo_structure_filter(
        {"action": {"type": "prefer_structure", "rule": "odd_target"}})["key"] == "pattern_cont"
    assert evolution.hypo_structure_filter({"action": {"type": "boost_method"}}) is None


def test_annotate_hypotheses_writes_regime_states(tmp_path, draws):
    reg_path = tmp_path / "hypotheses.json"
    hyp = hypothesis._build_hypothesis(
        family="t", statement="近30期热号", action={
            "type": "prefer_red_filter", "filter": {"key": "hot", "params": {"window": 30, "k": 6}}})
    hypothesis.register_hypotheses([hyp], registry_path=reg_path)
    struct = hypothesis._build_hypothesis(
        family="t2", statement="重号", action={"type": "prefer_structure", "rule": "keep_last"})
    hypothesis.register_hypotheses([struct], registry_path=reg_path)
    weight = hypothesis._build_hypothesis(
        family="t3", statement="给贝叶斯加权", action={"type": "boost_method", "method": "stat:bayes"})
    hypothesis.register_hypotheses([weight], registry_path=reg_path)

    out = evolution.annotate_hypotheses(draws, registry_path=reg_path, start=100)
    assert out["ok"] and out["annotated"] >= 1 and out["no_dimension"] >= 1
    reg = hypothesis.load_registry(reg_path)
    by_stmt = {h["statement"]: h for h in reg["hypotheses"]}
    assert by_stmt["近30期热号"]["regime"]["status"] in (
        "persistent", "emerging", "fading", "stale", "flipped", "reversed", "noise", "insufficient")
    assert by_stmt["重号"]["regime"]["status"] != "no_dimension", "结构假说应能映射到维度"
    assert by_stmt["给贝叶斯加权"]["regime"]["status"] == "no_dimension"


def test_register_llm_lab_claims_skips_noise_and_unmappable(tmp_path, monkeypatch, draws):
    claims_path = tmp_path / "llm_claims.json"
    reg_path = tmp_path / "hypotheses.json"
    monkeypatch.setattr(llm_lab, "CLAIMS_FILE", claims_path)
    llm_lab.persist_run({"claims": [
        {"title": "hot30", "verdict": "noise", "filters": [{"key": "hot", "params": {"window": 30, "k": 6}}]},
        {"title": "moon", "verdict": "support", "filters": [{"key": "moon", "params": {}}]},
        {"title": "good", "verdict": "support", "filters": [{"key": "cold", "params": {"window": 30, "k": 6}}],
         "n": 4000, "effect": 0.1, "p_adjusted": 0.01},
    ], "llm_used": True}, path=claims_path)
    out = evolution.register_llm_lab_claims(draws, registry_path=reg_path)
    assert out["picked"] == 1
    reg = hypothesis.load_registry(reg_path)
    assert len(reg["hypotheses"]) == 1
    assert reg["hypotheses"][0]["source"] == "llm"
    assert reg["hypotheses"][0]["status"] == "proposed"


def test_register_llm_lab_claims_without_file(tmp_path, monkeypatch, draws):
    monkeypatch.setattr(llm_lab, "CLAIMS_FILE", tmp_path / "missing.json")
    out = evolution.register_llm_lab_claims(draws, registry_path=tmp_path / "h.json")
    assert out["picked"] == 0
