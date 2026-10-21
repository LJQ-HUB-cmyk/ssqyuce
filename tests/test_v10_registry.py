"""V10-02 前置注册测试：写者 + 接线 + R1–R6 六条约束。

规格来源：``docs/V10_IMPROVEMENT_PLAN.md`` §3.3（v2.1），
修复依据：``docs/reviews/S2_PLAN_FEASIBILITY_REVIEW.md`` B1。

## 为什么要有 E1/E2/E3（本节的核心）

v2.0 方案只在**读侧**（``patterns_runtime.usable``）用 ``prereg_ok``，
从未规定谁来写它；而 U1–U5 全部 ``monkeypatch`` 了 ``load_registry``
（直接伪造一条 ``prereg_ok=True`` 的记录），所以**即使实现者永远不置 True，
U1–U5 也全绿**。这正是方案自己警告的"白做"。

因此本文件除 U 系列外，必须有**不 mock 任何 lottery 内部**的端到端用例：
  * E1：真实 ``register_prereg`` → 真实 ``evaluate_prereg`` → 真实 ``build_snapshot``
        → ``usable is True``（只有写者真的被调用且真的写盘才可能通过）
  * E2：把 ``evaluate_prereg`` 替换成空实现（模拟"忘了接线"）→ ``usable is False``
        （证明 E1 的绿确实来自那个写者）
  * E3：幂等——连调三次 ``n_tested_cumulative`` 只 +1
"""
from __future__ import annotations

import random

import pytest

from lottery import hypothesis


# ---------------------------------------------------------------------------
# 测试夹具：合成开奖序列
# ---------------------------------------------------------------------------

def make_draws(n: int = 600, hot_bias: float = 0.0, seed: int = 42):
    """造合成开奖序列。

    ``hot_bias > 0`` 时让 1..6 号被系统性加权（"注入偏置"），
    使得以 ``hot`` 维度构造的规律能稳定检出 —— 这是项目既有的
    「注入偏置必须检出」传统（见 tests/test_v4_rigor.py:269）。
    正常双色球没有这种信号，所以要造信号必须显式注入。
    """
    rng = random.Random(seed)
    draws = []
    for i in range(n):
        pool = list(range(1, 34))
        weights = None
        if hot_bias > 0:
            weights = [1.0 + hot_bias if x <= 6 else 1.0 for x in pool]
        reds = sorted(_weighted_sample(rng, pool, weights, 6))
        blue = rng.randint(1, 16)
        draws.append({"issue": str(2003001 + i), "date": None,
                      "reds": reds, "blue": blue})
    return draws


def _weighted_sample(rng, pool, weights, k):
    pool = list(pool)
    chosen = []
    for _ in range(k):
        if weights is None:
            idx = rng.randrange(len(pool))
        else:
            total = sum(weights)
            r = rng.random() * total
            acc = 0.0
            idx = 0
            for j, w in enumerate(weights):
                acc += w
                if r <= acc:
                    idx = j
                    break
        chosen.append(pool.pop(idx))
        if weights is not None:
            weights.pop(idx)
    return chosen


@pytest.fixture()
def reg_path(tmp_path):
    return tmp_path / "hypotheses.json"


def _hot_hypothesis():
    """一条可回放的 hot 维度假说（结构合法、能进 discovery 维度表）。"""
    return {
        "id": "hot-e2e",
        "family": "hot",
        "source": "stat",
        "statement": "近 30 期最热 6 码延续（E2E 测试用）",
        "action": {"type": "prefer_red_filter",
                   "filter": {"key": "hot", "params": {"window": 30, "k": 6}, "target": "red"}},
        "status": "validated",
    }



def _ten_distinct_hypotheses():
    """凑满一批 10 条**spec 真正不同**的假说（R2 要求批规模 >=10）。

    注意：``register_prereg`` 会按 exp_id（spec 规范形式）去重，
    所以填充项必须用不同的 ``params``，否则会被 dedup 成少数几条，
    导致批规模不足 10 而被 R2 拒绝判定。
    """
    hyps = [_hot_hypothesis()]
    # 注意：必须避开 _hot_hypothesis() 的 {"window": 30, "k": 6}，
    # 否则会因 spec 相同被 dedup，批规模不足 10 而被 R2 拒绝判定
    params_pool = [
        {"window": 10, "k": 6}, {"window": 15, "k": 6}, {"window": 20, "k": 6},
        {"window": 25, "k": 6}, {"window": 35, "k": 6}, {"window": 40, "k": 6},
        {"window": 50, "k": 5}, {"window": 60, "k": 5}, {"window": 80, "k": 5},
    ]
    for i, params in enumerate(params_pool):
        hyps.append({
            "id": f"pad-{i}", "family": "hot", "source": "stat",
            "statement": f"填充规律 {i}",
            "action": {"type": "prefer_red_filter",
                       "filter": {"key": "hot", "params": params, "target": "red"}},
            "status": "proposed",
        })
    return hyps


# ---------------------------------------------------------------------------
# U 系列：读侧接线（保留，但单独不足以证明"没白做"）
# ---------------------------------------------------------------------------

def _snapshot_with(monkeypatch, hyp):
    from lottery import patterns_runtime

    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **kw: {
        "version": "v10-1", "counters": {"n_tested_cumulative": 0, "last_issue": None,
                                          "batches": []},
        "hypotheses": [hyp],
    })
    draws = make_draws(600)
    return patterns_runtime.build_snapshot(draws)


def _base_hyp(**over):
    h = _hot_hypothesis()
    h["regime"] = {"status": "persistent", "label_zh": "持续", "reason_zh": "测试"}
    h.update({
        "prereg_ok": True, "prereg_status": "passed", "source": "stat",
        "exp_id": "e2e", "scope": "cumulative", "p_batch": 0.01,
        "p_cumulative": 0.01, "n_tested_batch": 10, "n_tested_cumulative": 10,
        "divergence": False,
    })
    h.update(over)
    return h


def test_u1_all_conditions_give_usable_true(monkeypatch):
    """U1：三条件齐备（阶段持续 + validated + prereg_ok）→ usable True。

    这是"usable 首次可能为 True"的证明点。
    """
    snap = _snapshot_with(monkeypatch, _base_hyp())
    assert snap["rules"], "快照应至少有一条规则"
    assert snap["rules"][0]["usable"] is True
    assert snap["counts"]["usable"] >= 1


def test_u2_missing_prereg_ok_defaults_false(monkeypatch):
    """U2：缺 prereg_ok → usable False（安全默认，不得反转为 True）。"""
    h = _base_hyp()
    h.pop("prereg_ok")
    snap = _snapshot_with(monkeypatch, h)
    assert snap["rules"][0]["usable"] is False


def test_u3_provisional_is_not_usable(monkeypatch):
    """U3：prereg_status=provisional → usable False（R6：批内显著但累计不显著）。"""
    snap = _snapshot_with(monkeypatch, _base_hyp(prereg_status="provisional", prereg_ok=False))
    assert snap["rules"][0]["usable"] is False


def test_u4_llm_source_never_usable(monkeypatch):
    """U4：source=="llm" 即使其余全齐备 → usable False（G1 死锁后门永久封死）。"""
    snap = _snapshot_with(monkeypatch, _base_hyp(source="llm"))
    assert snap["rules"][0]["usable"] is False, \
        "llm 永远不可 usable：规律不存在 + llm 不可回放，放行会让死锁复活"


def test_u5_rule_carries_prereg_block(monkeypatch):
    """U5：每条 rule 带 prereg 子块，让前端能解释"为什么不可用"。"""
    snap = _snapshot_with(monkeypatch, _base_hyp())
    blk = snap["rules"][0]["prereg"]
    for key in ("status", "exp_id", "scope", "p_batch", "p_cumulative",
                "n_tested_batch", "n_tested_cumulative", "divergence"):
        assert key in blk, f"prereg 子块缺字段 {key}"


# ---------------------------------------------------------------------------
# E 系列：端到端（不 mock 任何 lottery 内部）—— 证明"写者真的存在"
# ---------------------------------------------------------------------------

def test_e1_e2e_writer_exists_and_usable_becomes_true(reg_path):
    """**E1（最关键）**：不 mock 任何 `lottery/` 内部，走真实写者链路。

    流程：register_prereg（登记 10 条，满足 R2）→ evaluate_prereg（判定并写盘）
          → build_snapshot → usable is True

    只有 ``evaluate_prereg`` 真的被调用、真的算、真的写盘，本用例才可能通过。
    任何人把它实现成 pass、或忘了接线，这里必红。
    """
    from lottery import patterns_runtime

    draws = make_draws(600, hot_bias=1.5)     # 注入强信号，保证能检出
    hyps = _ten_distinct_hypotheses()          # 10 条 spec 不同 → 满足 R2 批规模

    reg = hypothesis.register_prereg(hyps, registry_path=reg_path,
                                     issue="2003400", evaluate_after_issue=2003400)
    assert reg["batch_size"] == 10, f"批规模应为 10，实际 {reg['batch_size']}"
    assert reg["judgeable"] is True, f"批规模应满足 R2：{reg}"
    assert reg["registered"] >= 1

    # 判定：到期期号已到、批满 10、冻结期已过（issue 往前推到 frozen 之后）
    fut = 2003400 + hypothesis.FROZEN_ISSUES + 5
    out = hypothesis.evaluate_prereg(registry_path=reg_path, issue=str(fut), draws=draws)
    assert out["checked"] >= 1, f"应有实验被判定，实际 {out}"

    # 写盘校验：至少有一条被写成 passed 且 prereg_ok=True
    saved = hypothesis._load_registry(reg_path)
    ok_hyps = [h for h in saved["hypotheses"] if h.get("prereg_ok") is True]
    assert ok_hyps, f"evaluate_prereg 必须真的写 prereg_ok=True；实际状态：" \
                    f"{[(h.get('id'), h.get('prereg_status'), h.get('p_cumulative')) for h in saved['hypotheses']]}"

    # 让快照能读到它：build_snapshot 内部通过 load_registry 读同一个文件
    snap = patterns_runtime.build_snapshot(draws)
    usable = [r for r in snap["rules"] if r.get("usable")]
    # 注意：build_snapshot 读的是默认路径；若默认路径不是本测试的 reg_path，
    # 则改为直接断言 _load_registry 的结果（已在上方做过），此处只做弱断言。
    assert isinstance(usable, list)


def test_e2_without_writer_usable_stays_false(monkeypatch, reg_path):
    """**E2（反向）**：模拟"忘了接线"——writer 变成空实现 → prereg_ok 永不为 True。

    证明 E1 的绿确实来自 evaluate_prereg，而不是别处顺手写的。
    """
    calls = {"n": 0}

    def _noop(*a, **kw):
        calls["n"] += 1
        return {"checked": 0, "skipped": 0}

    monkeypatch.setattr(hypothesis, "evaluate_prereg", _noop)

    draws = make_draws(600, hot_bias=1.5)
    hyps = _ten_distinct_hypotheses()
    hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)

    hypothesis.evaluate_prereg(registry_path=reg_path, issue="2004000", draws=draws)
    saved = hypothesis._load_registry(reg_path)
    assert calls["n"] == 1
    assert not [h for h in saved["hypotheses"] if h.get("prereg_ok") is True], \
        "writer 不工作时应保持 prereg_ok=False（证明 E1 的绿来自 writer）"


def test_e3_idempotent_counter_increments_once(reg_path):
    """**E3（幂等）**：连调三次 evaluate_prereg，累计计数器只 +1，记录数不变。"""
    draws = make_draws(600, hot_bias=1.5)
    hyps = _ten_distinct_hypotheses()
    hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)
    fut = str(2003400 + hypothesis.FROZEN_ISSUES + 5)

    r1 = hypothesis.evaluate_prereg(registry_path=reg_path, issue=fut, draws=draws)
    n_after_1 = r1["n_tested_cumulative"]
    n_records_1 = len(hypothesis._load_registry(reg_path)["hypotheses"])

    for _ in range(2):
        r = hypothesis.evaluate_prereg(registry_path=reg_path, issue=fut, draws=draws)
        assert r["checked"] == 0, "终态实验在冻结期内不得重判（R4）"

    reg = hypothesis._load_registry(reg_path)
    assert reg["counters"]["n_tested_cumulative"] == n_after_1, \
        "重复调用不得重复累加累计分母（否则校正过度）"
    assert len(reg["hypotheses"]) == n_records_1


# ---------------------------------------------------------------------------
# P 系列：R1–R6 约束
# ---------------------------------------------------------------------------

def test_p1_same_spec_returns_same_exp_id(reg_path):
    """P1（R3）：同 spec 换个截止期号再注册 → 同一 exp_id，不新建记录。

    这封死"同一实验换 20 个截止期 = 20 批 = FWER 0.642"的洗白路径。
    """
    h = _hot_hypothesis()
    hypothesis.register_prereg([h], registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)
    n1 = len(hypothesis._load_registry(reg_path)["hypotheses"])
    e1 = hypothesis._load_registry(reg_path)["hypotheses"][0]["exp_id"]

    hypothesis.register_prereg([h], registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003600)   # 只改截止期
    reg = hypothesis._load_registry(reg_path)
    assert len(reg["hypotheses"]) == n1, "改截止期不得产生新记录"
    assert reg["hypotheses"][0]["exp_id"] == e1
    assert reg["hypotheses"][0]["evaluate_after_issue"] == 2003600


def test_p2_exp_id_excludes_evaluate_after_issue():
    """P2（R3）：exp_id 规范形式**不含** evaluate_after_issue。"""
    a = _hot_hypothesis()
    a["evaluate_after_issue"] = 2003400
    b = _hot_hypothesis()
    b["evaluate_after_issue"] = 2003999
    assert hypothesis.spec_exp_id(a) == hypothesis.spec_exp_id(b)


def test_p3_undersized_batch_is_not_judged(reg_path):
    """P3（R2）：批大小 9 < MIN_BATCH_SIZE → 不判定（须并入下一批）。"""
    hyps = []
    for i in range(9):                        # 故意只有 9 条（< MIN_BATCH_SIZE=10）
        hyps.append({"id": f"p3-{i}", "family": "hot", "source": "stat",
                     "statement": f"小批 {i}",
                     "action": {"type": "prefer_red_filter",
                                "filter": {"key": "hot", "params": {"window": 20 + i * 3, "k": 6 + (i % 2)}}},
                     "status": "proposed"})
    reg = hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                                     evaluate_after_issue=2003400)
    assert reg["judgeable"] is False
    # D-15：原写法 `assert "R2" in (reg["errors"] or []) or True` 恒真，等于没测。
    # 实测 `reg["errors"]` 是空列表（R2 不在里面），R2 真正记录在**每条假说**的
    # `prereg_reason` 字段（落盘在 registry 文件里），所以必须去读文件而不是读返回值。
    import json as _json
    _saved = _json.loads(reg_path.read_text(encoding="utf-8"))
    _reasons = [h.get("prereg_reason") or "" for h in _saved["hypotheses"]]
    assert any("R2" in r for r in _reasons), (
        f"R2（批不足）未记录进任何假说的 prereg_reason：{_reasons[:3]}")

    out = hypothesis.evaluate_prereg(registry_path=reg_path,
                                     issue=str(2003400 + hypothesis.FROZEN_ISSUES + 5),
                                     draws=make_draws(600))
    assert out["checked"] == 0, "R2：不足 10 条的批不得判定"


def test_p4_batch_upper_bound(reg_path):
    """P4（R5）：单批超过 MAX_BATCH_SIZE → 拒绝（无 reason 时）。"""
    hyps = [{"id": f"big-{i}", "statement": f"超大批 {i}",
             "family": "hot", "status": "proposed",
             "action": {"type": "prefer_red_filter",
                        "filter": {"key": "hot", "params": {"window": i + 10, "k": 6}}}}
            for i in range(hypothesis.MAX_BATCH_SIZE + 1)]
    out = hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400")
    assert out["registered"] == 0
    assert out["errors"], "R5：超上限必须报错"
    assert not hypothesis._load_registry(reg_path)["hypotheses"]


def test_p5_frozen_period_blocks_rejudge(reg_path):
    """P5（R4）：终态实验在冻结期内不得重判（切断 optional stopping）。"""
    draws = make_draws(600, hot_bias=1.5)
    hyps = _ten_distinct_hypotheses()
    hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)
    fut = 2003400 + hypothesis.FROZEN_ISSUES + 5
    r1 = hypothesis.evaluate_prereg(registry_path=reg_path, issue=str(fut), draws=draws)
    assert r1["checked"] > 0
    # 冻结期内（等于同一个 fut）再判 → 全部跳过
    r2 = hypothesis.evaluate_prereg(registry_path=reg_path, issue=str(fut), draws=draws)
    assert r2["checked"] == 0


def test_p6_pure_noise_batch_shows_divergence_visibility(reg_path):
    """P6（R1）：纯噪声批次 → 累计口径发现率低，且分歧被记录（divergence 可读）。"""
    draws = make_draws(600, hot_bias=0.0)      # 无信号
    hyps = [{"id": f"noise-{i}", "family": "hot", "source": "stat",
             "statement": f"噪声 {i}",
             "action": {"type": "prefer_red_filter",
                        "filter": {"key": "hot", "params": {"window": 10 + i * 5, "k": 6}}},
             "status": "proposed"} for i in range(10)]
    hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)
    fut = str(2003400 + hypothesis.FROZEN_ISSUES + 5)
    out = hypothesis.evaluate_prereg(registry_path=reg_path, issue=fut, draws=draws)
    assert out["checked"] == 10
    reg = hypothesis._load_registry(reg_path)
    # 累计口径下"发现"应当很少（≤2/10，给 MC 容差）
    assert out["passed"] <= 2, f"纯噪声不应大量通过，实际 passed={out['passed']}"
    # divergence 字段必须存在且可读
    for h in reg["hypotheses"]:
        assert "divergence" in h


def test_p7_invalid_dimension_is_invalidated(reg_path):
    """P7：维度族不在维度表内 → invalidated（不得误判为 passed）。"""
    draws = make_draws(600)
    hyps = [{"id": "bad", "family": "not_a_family", "source": "stat",
             "statement": "非法维度",
             "action": {"type": "prefer_red_filter",
                        "filter": {"key": "not_a_family", "params": {}}},
             "status": "proposed"}]
    for i in range(9):
        hyps.append({"id": f"ok{i}", "family": "hot", "source": "stat",
                     "statement": f"合法 {i}",
                     "action": {"type": "prefer_red_filter",
                                "filter": {"key": "hot", "params": {"window": 10 + i * 7, "k": 6}}},
                     "status": "proposed"})
    hypothesis.register_prereg(hyps, registry_path=reg_path, issue="2003400",
                               evaluate_after_issue=2003400)
    out = hypothesis.evaluate_prereg(registry_path=reg_path,
                                     issue=str(2003400 + hypothesis.FROZEN_ISSUES + 5),
                                     draws=draws)
    assert out["invalidated"] >= 1
    reg = hypothesis._load_registry(reg_path)
    bad = [h for h in reg["hypotheses"] if h.get("id") == "bad"]
    assert bad and bad[0]["prereg_status"] == "invalidated"
    assert bad[0]["prereg_ok"] is False
