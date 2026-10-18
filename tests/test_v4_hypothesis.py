"""V5-2：LLM 规律提炼器 + 假说库（``lottery/hypothesis.py``）契约测试。

本文件锁定四条不能被破坏的契约：
1. **统计挖掘不依赖 LLM**：``config.LLM_DISABLED=True`` 时依然产出可实战验证的假说；
2. **LLM 全部失败路径优雅降级**：停用/未配置/超时/抛 ``LLMChannelError``/
   抛任意异常/返回 None/返回垃圾结构 —— 一律不抛给调用方，``llm_used=False``
   且 ``llm_error`` 非空；
3. **LLM 输出不可信**：缺 ``statement``/``action``、动作不在白名单、证据无样本量、
   或属「彩票是随机的所以无法预测」这类不可实战验证的表述，一律丢弃并记录原因；
4. **生命周期纪律**：样本量低于门槛不下支持/反对结论；``rejected`` 的假说
   绝不出现在 ``active_hypotheses`` 里，也不因单次支持自动复活。

协作边界：``battle.py`` 的 report 一律用**构造的 dict**，不依赖队友模块真实存在
（仅在其可导入时补一条集成冒烟，且用 skipif 守卫）。
"""
from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import pytest

from lottery import config
from lottery import hypothesis as H
from lottery import llm_client

# --------------------------------------------------------------------------
# 夹具与小工具
# --------------------------------------------------------------------------


def _mk_draws(n: int = 400, seed: int = 7, structured_tail: int = 0):
    """合成 n 期开奖；``structured_tail`` > 0 时把尾部若干期固定成同一组号码。

    尾部高度结构化是**故意**的：它让「近窗频率偏移」这类统计检验真的能拒绝
    均匀假设，从而验证「有信号才产出该族假说」这条路径（而不是永远无产出）。
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        if structured_tail and i >= n - structured_tail:
            reds, blue = [1, 2, 3, 4, 5, 6], 1
        else:
            reds = sorted(rng.sample(range(1, 34), 6))
            blue = rng.randint(1, 16)
        out.append({
            "issue": f"{2026001 + i}",
            "date": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "reds": reds,
            "blue": blue,
        })
    return out


def _per_issue(n: int, red: float, blue: float = 0.0, seed: int = 3,
               drift: float = 0.0):
    """构造逐期明细（字段名与 ``battle.replay_method`` 的 ``per_issue`` 一致）。

    ``blue`` 为**该期是否命中蓝球的比例**（0~1）：真实回放里 ``blue_hit`` 是
    「该期所有注的蓝球命中数」四舍五入后的整数，所以这里按 ``blue`` 决定每期
    ``blue_hit`` 取 1 还是 0 —— 覆盖方案与独立重复方案在「至少中 1 蓝」上的
    差别正是靠这个整数体现的。
    ``drift`` > 0 时让红球命中数随时间线性上移（模拟「方法在近期变好」），
    供「方法趋势」族使用。
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        shift = drift * (i / max(1, n - 1) - 0.5) * 2.0
        out.append({
            "issue": f"{2026001 + i}",
            "red_hits": max(0, int(round(red + shift + (rng.random() - 0.5) * 1.6))),
            "blue_hit": 1 if rng.random() < blue else 0,
            "prize_level": 6,
            "reward": 0.0,
            "n_tickets": 10,
        })
    return out


def _mk_report(n_issues: int = 300, min_issues: int = 60, with_per_issue: bool = True):
    """构造一份与 ``battle.battle_report()`` 同形的报告 dict（不 import battle）。"""
    rows = []
    # stat:freq 带正向漂移（近期变好）→ 必然触发 method_trend_up；
    # uniform 无漂移 → 作为对照不产生趋势假说。
    for name, red, blue, drift in (("stat:freq", 1.16, 0.062, 0.5),
                                   ("uniform", 1.09, 0.060, 0.0),
                                   ("blue_coverage", 1.09, 0.35, 0.0)):
        row = {
            "method": name, "replayable": True, "reason": None,
            "issues": n_issues, "n_tickets_total": n_issues * 10,
            "red_hits_mean": red, "blue_hit_rate": blue,
            "prize_rate_ge5": 0.03, "reward_total": 0.0,
            "cost_total": float(n_issues * 10 * 2), "roi": -0.5,
        }
        if with_per_issue:
            row["per_issue"] = _per_issue(n_issues, red, blue, drift=drift)
        rows.append(row)
    return {
        "version": "v5-1",
        "n_issues": n_issues,
        "min_issues": min_issues,
        "gate_zh": "可做显著性判决",
        "results": rows,
        "comparison": {
            "baseline_method": "uniform",
            "top_method": "stat:freq",
            "min_issues": min_issues,
            "pairs": [{
                "pair": "stat:freq_vs_uniform", "method": "stat:freq",
                "vs": "uniform", "n_issues": n_issues,
                "delta_red_hits": 0.07, "delta_blue_rate": 0.002,
                "p_value": 0.02, "p_adjusted": 0.04, "verdict": "显著更优",
            }],
        },
    }


def _hyp(statement: str, family: str = "f", action=None, baseline=1.09):
    """构造一条结构完整的待注册假说。"""
    return {
        "family": family, "statement": statement,
        "action": action or {"type": "prefer_numbers", "numbers": [1, 2]},
        "params": {}, "evidence": {"n": 300, "lift": 0.03, "baseline": baseline},
        "source": "stat", "n_samples": 300,
    }


@pytest.fixture()
def reg_path(tmp_path: Path) -> Path:
    """隔离的假说库路径（**绝不写仓库 data/**）。"""
    return tmp_path / "hypotheses.json"


@pytest.fixture()
def llm_spy(monkeypatch):
    """替换 ``llm_client.chat_json`` 并记录调用，避免任何真实网络访问。

    返回 ``(install, calls)``；``install`` 接受「返回值」或「要抛出的异常实例」。
    """
    calls = {"n": 0, "system": None, "user": None, "kwargs": None}

    def _install(result):
        def _fake(system, user, **kwargs):
            calls["n"] += 1
            calls["system"] = system
            calls["user"] = user
            calls["kwargs"] = kwargs
            if isinstance(result, BaseException):
                raise result
            return result
        monkeypatch.setattr(llm_client, "chat_json", _fake)
        return calls

    return _install, calls


# --------------------------------------------------------------------------
# 1. 统计挖掘：不依赖 LLM 也能产出可实战验证的假说
# --------------------------------------------------------------------------


def test_stat_mining_works_with_llm_disabled(monkeypatch):
    """``config.LLM_DISABLED=True`` 时仍产出统计假说，且 llm_used=False。"""
    monkeypatch.setattr(config, "LLM_DISABLED", True)
    res = H.mine_hypotheses(_mk_report(), _mk_draws(400), use_llm=True)
    assert res["llm_used"] is False
    assert res["llm_count"] == 0
    assert res["llm_error"]
    assert res["stat_count"] >= 5
    assert len(res["hypotheses"]) == res["stat_count"]
    assert all(h["source"] == "stat" for h in res["hypotheses"])


def test_every_stat_hypothesis_has_executable_whitelisted_action():
    """每条统计假说都必须带 action，动作类型在白名单内，且证据含样本量与差值。"""
    res = H.mine_hypotheses(_mk_report(), _mk_draws(400), use_llm=False)
    assert res["hypotheses"], "统计挖掘不应产出空列表"
    for h in res["hypotheses"]:
        act = h.get("action")
        assert isinstance(act, dict), f"{h['family']} 缺 action"
        assert act.get("type") in H.ALLOWED_ACTION_TYPES, h
        ev = h["evidence"]
        assert "n" in ev and int(ev["n"]) > 0, h
        assert any(k in ev for k in ("lift", "observed", "p_value")), h
        assert h["n_samples"] > 0
        assert h["statement"] and h["family"] and h["id"] and h["created_at"]
        assert h["source"] == "stat"


def test_stat_mining_covers_multiple_families():
    """统计挖掘应覆盖多个规律族，且真有信号时频率偏移族必须被检出。"""
    draws = _mk_draws(400, structured_tail=60)
    res = H.mine_hypotheses(_mk_report(), draws, use_llm=False)
    fams = set(res["families"])
    assert len(fams) >= 5, fams
    assert "freq_offset_red" in fams, fams          # 有信号 → 必须检出
    assert "freq_offset_blue" in fams, fams         # 蓝球 1 被固定 → 必然偏移
    # 遗漏 / 重号 / 方法趋势族在正常样本下必须出现
    assert {"omission_ratio_red", "omission_ratio_blue", "repeat_confirmed"} <= fams
    assert any(f.startswith("method_trend") for f in fams), fams


def test_frequency_family_is_silent_on_pure_noise():
    """纯噪声（无结构）时不应凭噪声产出「频率偏移」假说——检验门槛生效。"""
    res = H.mine_hypotheses(_mk_report(), _mk_draws(400, seed=11), use_llm=False)
    assert "freq_offset_red" not in res["families"]


def test_blue_structural_family_uses_replay_evidence():
    """蓝球结构族从回放逐期明细得出「至少中 1 蓝」的实测占比与提升。"""
    res = H.mine_hypotheses(_mk_report(), _mk_draws(400), use_llm=False)
    fams = {h["family"]: h for h in res["hypotheses"]}
    assert "blue_dedupe_coverage" in fams
    hyp = fams["blue_dedupe_coverage"]
    assert hyp["action"] == {"type": "blue_dedupe", "enabled": True}
    ev = hyp["evidence"]
    assert ev["n"] > 0 and "lift" in ev and "observed" in ev and "baseline" in ev
    # 覆盖方案「每期至少中 1 蓝」的期数占比必须高于独立重复方案
    assert float(ev["observed"]) > float(ev["baseline"])
    assert float(ev["lift"]) > 0.2


def test_blue_uniform_fallback_when_coverage_is_worse():
    """覆盖方案逐注蓝球命中率低于均匀方案时，产出「蓝球回退均匀」假说。"""
    report = _mk_report(with_per_issue=False)
    for row in report["results"]:
        if row["method"] == "blue_coverage":
            row["blue_hit_rate"] = 0.01
        elif row["method"] == "uniform":
            row["blue_hit_rate"] = 0.06
    res = H.mine_hypotheses(report, _mk_draws(400), use_llm=False)
    fams = {h["family"]: h for h in res["hypotheses"]}
    assert "blue_uniform_fallback" in fams
    assert fams["blue_uniform_fallback"]["action"] == {"type": "blue_uniform",
                                                       "enabled": True}


def test_empty_inputs_do_not_crash():
    """空 report / 空 draws / 全非法 draws 都不崩，返回可用的空结果。"""
    for report, draws in (({}, []), (None, None), ({"results": []}, []),
                          ({"results": "oops"}, [{"reds": [1, 2], "blue": 99}]),
                          ({"results": [{"method": "x"}]}, None)):
        res = H.mine_hypotheses(report, draws, use_llm=False)
        assert res["stat_count"] == 0
        assert res["hypotheses"] == []
        assert res["llm_used"] is False
        assert res["families"] == []


def test_mine_hypotheses_is_pure_no_write(tmp_path, monkeypatch):
    """``mine_hypotheses`` 是纯函数：不写盘、不建目录、不动默认 registry 路径。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    H.mine_hypotheses(_mk_report(), _mk_draws(300), use_llm=False)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    assert not (tmp_path / "data").exists()


def test_registry_default_path_derives_from_data_dir(monkeypatch, tmp_path):
    """默认 registry 路径从 ``config.DATA_DIR`` 派生，不硬编码绝对路径。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assert Path(H.registry_path()) == tmp_path / "data" / "hypotheses.json"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "other")
    assert Path(H.registry_path()) == tmp_path / "other" / "hypotheses.json"


# --------------------------------------------------------------------------
# 2. LLM 失败路径：一律优雅降级
# --------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [
    "channel_error", "runtime_error", "timeout", "returns_none", "http_style_message",
])
def test_llm_failures_degrade_gracefully(monkeypatch, llm_spy, failure):
    """通道异常 / 任意异常 / 超时 / 返回 None —— 都不抛，llm_used=False，统计产出保留。"""
    install, _ = llm_spy
    if failure == "channel_error":
        install(llm_client.LLMChannelError("LLM 调用失败: HTTP 503"))
    elif failure == "runtime_error":
        install(RuntimeError("connection reset by peer"))
    elif failure == "timeout":
        install(llm_client.LLMChannelError("LLM 总耗时超限（超出本次预测的大模型时间预算）"))
    elif failure == "http_style_message":
        install(llm_client.LLMChannelError("LLM 调用失败: HTTP 429: rate limited"))
    else:
        install(None)

    res = H.mine_hypotheses(_mk_report(), _mk_draws(300), use_llm=True)
    assert res["llm_used"] is False
    assert res["llm_count"] == 0
    assert isinstance(res["llm_error"], str) and res["llm_error"]
    assert res["stat_count"] > 0                        # 统计假说仍然产出
    assert all(h["source"] == "stat" for h in res["hypotheses"])


def test_llm_disabled_short_circuits_without_calling_channel(monkeypatch, llm_spy):
    """``LLM_DISABLED=True`` 时直接跳过 LLM，连一次调用都不发起。"""
    install, calls = llm_spy
    monkeypatch.setattr(config, "LLM_DISABLED", True)
    install({"hypotheses": [{"statement": "x", "action": {"type": "blue_uniform"},
                             "evidence": {"n": 1, "lift": 0}}]})
    res = H.mine_hypotheses(_mk_report(), _mk_draws(300), use_llm=True)
    assert calls["n"] == 0                                # 一次调用都没发起
    assert res["llm_used"] is False
    assert "停用" in res["llm_error"] and "LLM_DISABLED" in res["llm_error"]
    assert res["stat_count"] > 0


def test_ask_llm_never_raises_on_any_exception(llm_spy):
    """直接打 ``_ask_llm``：任何异常都被兜住，返回 (空假说, 错误串, 拒绝列表)。"""
    install, _ = llm_spy
    for exc in (ValueError("模型返回了非 JSON"), KeyError("choices"),
                ConnectionError("network unreachable"),
                llm_client.LLMChannelError("通道未配置")):
        install(exc)
        hyps, err, rejected = H._ask_llm(_mk_report(), None, [])
        assert hyps == [] and isinstance(err, str) and err
        assert rejected == []


# --------------------------------------------------------------------------
# 3. LLM 输出结构校验 + 动作白名单
# --------------------------------------------------------------------------


def test_llm_output_is_validated_and_filtered(llm_spy):
    """缺字段 / 动作不在白名单 / 无依据 / 不可验证表述 —— 逐一丢弃并记录原因。"""
    install, _ = llm_spy
    install({"hypotheses": [
        {"family": "ok", "statement": "近 300 期蓝球 3 偏低，蓝球覆盖时优先纳入。",
         "action": {"type": "blue_prefer", "blues": [3, 9]},
         "evidence": {"n": 300, "lift": 0.02}},
        {"action": {"type": "prefer_numbers", "numbers": [1]}},               # 缺 statement
        {"statement": "缺动作的假说", "evidence": {"n": 300}},                 # 缺 action
        {"statement": "动作类型非法", "action": {"type": "nuke_everything"},
         "evidence": {"n": 300, "lift": 0.1}},                                # 白名单外
        {"statement": "无依据的假说", "action": {"type": "blue_dedupe"},
         "evidence": {}},                                                     # 无样本量
        {"statement": "彩票是随机的所以无法预测", "action": {"type": "blue_uniform"},
         "evidence": {"n": 9, "lift": 0.0}},                                  # 不可实战验证
        {"statement": "红球越界", "action": {"type": "prefer_numbers", "numbers": [99, 0]},
         "evidence": {"n": 300, "lift": 0.01}},                               # 号码越界 → 空
        "不是对象",                                                            # 类型错误
    ]})
    res = H.mine_hypotheses(_mk_report(), _mk_draws(300), use_llm=True)

    assert res["llm_used"] is True
    assert res["llm_count"] == 1
    assert res["llm_error"] is None
    llm_hyps = [h for h in res["hypotheses"] if h["source"] == "llm"]
    assert len(llm_hyps) == 1
    assert llm_hyps[0]["action"] == {"type": "blue_prefer", "blues": [3, 9]}

    rejected = "\n".join(res["llm_rejected"])
    assert len(res["llm_rejected"]) == 7
    assert "缺少 statement" in rejected
    assert "action 不是对象" in rejected
    assert "不在白名单" in rejected
    assert "evidence 缺少样本量" in rejected
    assert "不可实战验证" in rejected


def test_llm_action_normalization_clamps_and_cleans(llm_spy):
    """通过校验的动作仍要规范化：号码去重升序、权重钳制在 0.2~3.0。"""
    install, _ = llm_spy
    install({"hypotheses": [
        {"statement": "红球候选", "action": {"type": "prefer_numbers",
                                            "numbers": [7, 7, 33, 2]},
         "evidence": {"n": 200, "lift": 0.01}},
        {"statement": "方法加权", "action": {"type": "boost_method",
                                            "method": "stat:omission", "weight": 99},
         "evidence": {"n": 200, "lift": 0.01}},
        {"statement": "方法降权", "action": {"type": "boost_method",
                                            "method": "stat:markov", "weight": 0.001},
         "evidence": {"n": 200, "lift": 0.01}},
        {"statement": "空号码表", "action": {"type": "prefer_numbers", "numbers": []},
         "evidence": {"n": 200, "lift": 0.01}},
    ]})
    res = H.mine_hypotheses(_mk_report(), _mk_draws(300), use_llm=True)
    acts = {h["statement"]: h["action"] for h in res["hypotheses"] if h["source"] == "llm"}
    assert acts["红球候选"] == {"type": "prefer_numbers", "numbers": [2, 7, 33]}
    assert acts["方法加权"] == {"type": "boost_method", "method": "stat:omission",
                                "weight": H.MAX_METHOD_WEIGHT}
    assert acts["方法降权"]["weight"] == H.MIN_METHOD_WEIGHT
    assert "空号码表" not in acts
    assert any("有效红球" in r for r in res["llm_rejected"])


def test_llm_dedup_keeps_stat_version(llm_spy):
    """LLM 假说与统计假说同 ID 时保留统计版（统计版依据可复现），并自我去重。"""
    install, _ = llm_spy
    dup_stmt = "完全一样的一句话陈述"
    installation = {"hypotheses": [
        {"family": "dup", "statement": dup_stmt,
         "action": {"type": "blue_prefer", "blues": [4]},
         "evidence": {"n": 100, "lift": 0.01}},
        {"family": "dup", "statement": dup_stmt,
         "action": {"type": "blue_prefer", "blues": [4]},
         "evidence": {"n": 100, "lift": 0.01}},
    ]}
    install(installation)
    res = H.mine_hypotheses(_mk_report(), _mk_draws(200), use_llm=True)
    statements = [h["statement"] for h in res["hypotheses"]]
    assert statements.count(dup_stmt) == 1
    ids = [h["id"] for h in res["hypotheses"]]
    assert len(set(ids)) == len(ids)


def test_llm_prompt_carries_battle_and_stats_but_bans_randomness_talk(llm_spy):
    """提示词必须带战绩表 / 统计候选 / 白名单，并明确要求可实战验证。"""
    install, calls = llm_spy
    install({"hypotheses": []})
    draws = _mk_draws(200)
    H.mine_hypotheses(_mk_report(), draws, use_llm=True)
    assert calls["n"] == 1
    sys_prompt, user_prompt = calls["system"], calls["user"]
    assert "可实战验证" in sys_prompt
    for token in ("boost_method", "prefer_numbers", "blue_prefer",
                  "blue_dedupe", "blue_uniform"):
        assert token in sys_prompt
    assert "stat:freq" in user_prompt and "blue_coverage" in user_prompt   # 战绩表
    assert "统计挖掘已产出的候选规律" in user_prompt                          # 统计候选
    assert str(draws[-1]["issue"]) in user_prompt                           # 近期开奖
    assert "样本门槛" in user_prompt                                        # 样本量纪律


def test_llm_deadline_and_token_budget_are_passed(llm_spy):
    """每次 LLM 调用都必须带限时（deadline）与输出预算，且走非 strict 降级语义。"""
    install, calls = llm_spy
    install({"hypotheses": []})
    H.mine_hypotheses(_mk_report(), _mk_draws(200), use_llm=True,
                      max_tokens=1234, timeout=7.5)
    kw = calls["kwargs"]
    assert kw["deadline"] is not None
    assert kw["timeout"] == 7.5
    assert kw["max_tokens"] == 1234
    assert kw["strict"] is False


@pytest.mark.parametrize("payload", [
    "just a string", 42, {"foo": 1}, {"hypotheses": "nope"}, {"hypotheses": {}},
    {"hypotheses": None}, [1, 2, 3],
])
def test_llm_malformed_shapes_are_rejected(llm_spy, payload):
    """非 dict / 缺 hypotheses 键 / hypotheses 非数组 —— 全部按降级处理。"""
    install, _ = llm_spy
    install(payload)
    res = H.mine_hypotheses(_mk_report(), _mk_draws(200), use_llm=True)
    assert res["llm_used"] is False
    assert res["llm_error"]
    assert res["stat_count"] > 0


def test_llm_bare_list_is_accepted(llm_spy):
    """容错：LLM 直接返回裸数组时也要能正确解析。"""
    install, _ = llm_spy
    install([{"statement": "裸数组假说", "action": {"type": "blue_uniform"},
              "evidence": {"n": 120, "lift": 0.01}}])
    res = H.mine_hypotheses(_mk_report(), _mk_draws(200), use_llm=True)
    assert res["llm_count"] == 1 and res["llm_used"] is True


# --------------------------------------------------------------------------
# 4. 假说库：注册 / 打分 / 收敛 / 读取
# --------------------------------------------------------------------------


def test_register_is_idempotent_and_accumulates_attempts(reg_path):
    """同 statement 重复注册只累加 attempts，不新建记录。"""
    hyps = [
        _hyp("S1"), _hyp("S1"),                        # 同一 statement
        _hyp("S2", family="g"), _hyp("S3", family="f"),  # 同族不同陈述
        {"family": "bad", "statement": "动作非法", "action": {"type": "nope"}},
        {"family": "bad2", "action": {"type": "blue_dedupe"}},   # 缺 statement
    ]
    r1 = H.register_hypotheses(hyps, reg_path)
    assert r1["registered"] == 3 and r1["deduped"] == 1
    assert r1["total"] == 3 and r1["skipped"] == 2
    assert r1["registry_path"] == str(reg_path)
    r2 = H.register_hypotheses(hyps, reg_path)
    assert r2["registered"] == 0 and r2["deduped"] == 4 and r2["total"] == 3

    reg = H.load_registry(reg_path)
    by_stmt = {h["statement"]: h for h in reg["hypotheses"]}
    assert by_stmt["S1"]["attempts"] == 4
    assert by_stmt["S2"]["attempts"] == 2
    assert by_stmt["S1"]["status"] == "proposed"
    assert by_stmt["S1"]["supports"] == 0 and by_stmt["S1"]["refutes"] == 0
    ids = [h["id"] for h in reg["hypotheses"]]
    assert len(set(ids)) == len(ids)                 # 同族不同陈述不撞 ID
    assert all(h["status"] in H.HYPOTHESIS_STATUSES for h in reg["hypotheses"])


def test_register_empty_and_garbage_inputs(reg_path):
    """空列表 / 非 dict 条目不崩，也不写出坏记录。"""
    assert H.register_hypotheses([], reg_path)["total"] == 0
    out = H.register_hypotheses([None, 42, "x", {}], reg_path)
    assert out["registered"] == 0 and out["skipped"] == 4 and out["total"] == 0


def test_score_hypothesis_insufficient_sample():
    """样本量低于门槛一律判「样本不足」，哪怕差值很大。"""
    hyp = _hyp("样本测试")
    min_issues = config.METHOD_RECOMMENDATION_MIN_ISSUES
    s = H.score_hypothesis(hyp, {"n_issues": min_issues - 1, "delta_red_hits": 5.0})
    assert s["verdict"] == "样本不足"
    assert s["n_issues"] == min_issues - 1 and s["min_issues"] == min_issues
    assert "门槛" in s["basis"]
    assert set(s) >= {"hyp_id", "metric", "delta", "n_issues", "verdict"}
    # 刚好达标即转为可判决
    s2 = H.score_hypothesis(hyp, {"n_issues": min_issues, "delta_red_hits": 5.0})
    assert s2["verdict"] == "支持"


def test_score_hypothesis_support_refute_and_edges():
    """样本达标时按差值方向判决；差异过小或 p 不显著判无显著差异；空输入不崩。"""
    hyp = _hyp("方向测试")
    assert H.score_hypothesis(hyp, {"n_issues": 300, "delta_red_hits": 0.2})["verdict"] == "支持"
    assert H.score_hypothesis(hyp, {"n_issues": 300, "delta_red_hits": -0.2})["verdict"] == "反对"
    tiny = H.score_hypothesis(hyp, {"n_issues": 300, "delta_red_hits": 0.001})
    assert tiny["verdict"] == "无显著差异" and tiny["direction"] == "none"
    # 带校正 p 值时以 p 为准
    assert H.score_hypothesis(hyp, {"n_issues": 300, "delta_red_hits": 0.5,
                                    "p_adjusted": 0.4})["verdict"] == "无显著差异"
    assert H.score_hypothesis(hyp, {"n_issues": 300, "delta_red_hits": 0.5,
                                    "p_adjusted": 0.01})["verdict"] == "支持"
    # 用证据基线对比（无 delta_red_hits）
    by_base = H.score_hypothesis(_hyp("基线对比"), {"n_issues": 300, "red_hits_mean": 1.30})
    assert by_base["verdict"] == "支持" and by_base["delta"] > 0
    # 空 / 缺指标 → 样本不足，不崩
    assert H.score_hypothesis(hyp, {})["verdict"] == "样本不足"
    assert H.score_hypothesis({}, {"n_issues": 300})["verdict"] == "样本不足"
    assert H.score_hypothesis(None, None)["verdict"] == "样本不足"
    # n_issues 缺失时回退 per_issue 长度
    n_from_per = H.score_hypothesis(hyp, {"delta_red_hits": 0.3,
                                          "per_issue": [{"red_hits": 1}] * 80})
    assert n_from_per["n_issues"] == 80 and n_from_per["verdict"] == "支持"


def test_resolve_registry_migrates_status_and_excludes_rejected(reg_path):
    """按回放结果迁移状态：validated / rejected / 保持 proposed，都不出现在 active 里。"""
    H.register_hypotheses([_hyp("会被支持"), _hyp("会被否决"), _hyp("样本不足者")], reg_path)
    ids = [h["id"] for h in H.load_registry(reg_path)["hypotheses"]]

    out = H.resolve_registry([
        {"hyp_id": ids[0], "n_issues": 300, "delta_red_hits": 0.25},
        {"hyp_id": ids[1], "n_issues": 300, "delta_red_hits": -0.25},
        {"hyp_id": ids[2], "n_issues": 10, "delta_red_hits": 0.9},
    ], reg_path)
    assert out["validated"] == [ids[0]]
    assert out["rejected"] == [ids[1]]
    assert out["insufficient"] == [ids[2]]
    assert out["scored"] == 3

    reg = {h["id"]: h for h in H.load_registry(reg_path)["hypotheses"]}
    assert reg[ids[0]]["status"] == "validated"
    assert reg[ids[1]]["status"] == "rejected"
    assert reg[ids[2]]["status"] == "proposed"
    assert reg[ids[0]]["supports"] == 1 and reg[ids[1]]["refutes"] == 1

    active = {h["id"] for h in H.active_hypotheses(reg_path)}
    assert active == {ids[0]}
    assert ids[1] not in active and ids[2] not in active
    # 被否决的仍留档供复盘（不是删除）
    assert ids[1] in reg


def test_resolve_registry_rejected_does_not_revive(reg_path):
    """已被否决的假说不因单次支持自动复活（避免在噪声上来回抖动）。"""
    H.register_hypotheses([_hyp("已否决")], reg_path)
    rid = H.load_registry(reg_path)["hypotheses"][0]["id"]
    H.resolve_registry([{"hyp_id": rid, "n_issues": 300, "delta_red_hits": -0.3}], reg_path)
    assert H.load_registry(reg_path)["hypotheses"][0]["status"] == "rejected"
    back = H.resolve_registry([{"hyp_id": rid, "n_issues": 300, "delta_red_hits": 0.5}], reg_path)
    assert back["validated"] == []
    assert H.load_registry(reg_path)["hypotheses"][0]["status"] == "rejected"
    assert H.active_hypotheses(reg_path) == []


def test_resolve_registry_matches_by_method_name(reg_path):
    """回放结果没带 hyp_id 时，按 action.method 与战绩行关联。"""
    H.register_hypotheses([
        _hyp("方法趋势", family="method_trend_up",
             action={"type": "boost_method", "method": "stat:freq", "weight": 1.3}),
        _hyp("别的假说", family="prefer",
             action={"type": "prefer_numbers", "numbers": [5]}),
    ], reg_path)
    out = H.resolve_registry([
        {"method": "stat:freq", "n_issues": 300, "delta_red_hits": 0.3},
        {"method": "uniform", "n_issues": 300, "delta_red_hits": 0.3},
    ], reg_path)
    assert len(out["validated"]) == 1
    status = {h["statement"]: h["status"] for h in H.load_registry(reg_path)["hypotheses"]}
    assert status["方法趋势"] == "validated"
    assert status["别的假说"] == "proposed"     # 无匹配回放 → 不动状态


def test_resolve_registry_handles_empty_and_missing(reg_path, tmp_path):
    """空回放结果 / 不存在的库路径都不崩。"""
    empty = H.resolve_registry([], reg_path)
    # V12-21 补集反转（裁决 R-8）新增三个字段：
    #   complements / complements_added —— 负效应转成的补集候选
    #   n_tested_delta —— 恒为 0（补集是同一次检验的另一种写法，FWER 不变）
    assert empty == {"validated": [], "rejected": [], "insufficient": [],
                     "unchanged": 0, "scored": 0,
                     "complements": [], "complements_added": 0,
                     "n_tested_delta": 0,
                     "registry_path": str(reg_path)}
    H.register_hypotheses([_hyp("无匹配")], reg_path)
    assert H.resolve_registry(None, reg_path)["unchanged"] == 1
    assert H.resolve_registry([{"n_issues": 300}], tmp_path / "nope.json")["scored"] == 0


# --------------------------------------------------------------------------
# 5. 边界：库文件损坏 / 非法结构 / 落盘原子性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["{not json", "", "[]", "null", '"str"',
                                 "{\"hypotheses\": \"oops\"}",
                                 "{\"hypotheses\": [1, 2, null]}"])
def test_corrupted_registry_degrades_to_empty(reg_path, bad):
    """库文件损坏 / 结构非法时不崩：按空库继续，读取与写入都能正常工作。"""
    reg_path.write_text(bad, encoding="utf-8")
    assert H.load_registry(reg_path)["hypotheses"] == []
    assert H.active_hypotheses(reg_path) == []
    out = H.resolve_registry([{"n_issues": 300, "delta_red_hits": 1.0}], reg_path)
    assert out["scored"] == 0
    reg = H.register_hypotheses([_hyp("损坏后重建")], reg_path)
    assert reg["registered"] == 1
    assert json.loads(reg_path.read_text(encoding="utf-8"))["hypotheses"]


def test_registry_bare_list_shape_is_tolerated(reg_path):
    """库文件是裸数组形态时也能读出来（向后兼容）。"""
    reg_path.write_text(json.dumps([_hyp("裸数组库")], ensure_ascii=False),
                        encoding="utf-8")
    regs = H.load_registry(reg_path)["hypotheses"]
    assert len(regs) == 1 and regs[0]["statement"] == "裸数组库"


# --------------------------------------------------------------------------
# 6. 与 battle.py 的集成冒烟（仅在队友模块可导入时执行）
# --------------------------------------------------------------------------

_HAS_BATTLE = importlib.util.find_spec("lottery.battle") is not None


@pytest.mark.skipif(not _HAS_BATTLE, reason="lottery.battle 尚未就绪")
def test_integration_with_real_battle_report(tmp_path):
    """真实 ``battle.battle_report()`` 的输出能被直接消费（小样本、快）。"""
    from lottery import battle

    draws = _mk_draws(140, seed=5, structured_tail=20)
    report = battle.battle_report(draws, n_tickets=3, start=60, min_issues=10)
    assert "results" in report

    res = H.mine_hypotheses(report, draws, use_llm=False)
    for h in res["hypotheses"]:
        assert h["action"]["type"] in H.ALLOWED_ACTION_TYPES

    path = tmp_path / "hyp.json"
    assert H.register_hypotheses(res["hypotheses"], path)["total"] == res["stat_count"]
    # 用报告里真实的方法战绩行给假说打分
    first_playable = next(r for r in report["results"] if r.get("replayable"))
    scored = H.score_hypothesis(H.load_registry(path)["hypotheses"][0]
                               if res["hypotheses"] else {}, first_playable)
    assert scored["verdict"] in ("支持", "反对", "无显著差异", "样本不足")
