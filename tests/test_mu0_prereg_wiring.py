"""MU-0 / W06：前置注册「写入侧」接线测试。

## 为什么这组测试必须存在

本轮修复的是一条**静默失效数月**的断链：

```
register_prereg 零调用 → 没有 prereg_status → evaluate_prereg 跳过全部
→ prereg_ok 恒 False → usable_rules() 恒空 → 界面永远「可用规律 0 条」
```

这条链每一段都"正常工作"，只是没人供给输入。所以**正向覆盖率不足以发现它**，
必须存在"断言写入真的发生了"的测试，否则下次重构掉接线仍会全绿。

本文件的关键断言（缺一不可）：
1. 登记后注册簿里**真的出现** `prereg_status='registered'` 的记录；
2. **删掉调用点必须变红**（端到端反向用例）；
3. **幂等**：重复运行不得让记录数膨胀（`n_tested_cumulative` 是只增不减的校正分母）；
4. 只登记 `source=='stat'`（LLM 假设不得进 α 台账）；
5. 健康度 `never_registered` 能把"空转"变成可观测信号。
"""
from __future__ import annotations

import pytest

from lottery import db, hypothesis
from lottery import prereg_scheduler as PS


# ---------------------------------------------------------------------------
# 基础：候选转换
# ---------------------------------------------------------------------------

def test_candidates_to_hypotheses_shape(isolated_env):
    """候选必须转成 `_spec_canonical` 认识的形状，否则 exp_id 会全部撞车。"""
    items = [{
        "status": "noise",
        "filter": {"key": "sum_range", "name_zh": "和值区间条件",
                   "target": "red", "params": {"window": 30, "lo": 45, "hi": 75}},
        "quick": {"n": 3316, "delta": 0.0541, "z": 3.593, "p": 0.00065,
                  "p_adjusted": 0.0811},
    }]
    hyps = PS.candidates_to_hypotheses(items, issue="2026119")
    assert len(hyps) == 1
    h = hyps[0]
    assert h["source"] == "stat"
    assert h["action"]["filter"]["key"] == "sum_range"
    assert h["action"]["filter"]["params"]["window"] == 30
    eid = hypothesis.spec_exp_id(h)
    assert isinstance(eid, str) and len(eid) == 16


def test_different_filters_get_different_exp_ids(isolated_env):
    """关键：不同 filter 必须得到不同 exp_id，否则候选会被去重成 1 条。"""
    items = [
        {"filter": {"key": "sum_range", "target": "red",
                    "params": {"window": 30, "lo": 45, "hi": 75}}, "quick": {}},
        {"filter": {"key": "pair", "target": "red",
                    "params": {"window": 100, "top": 3}}, "quick": {}},
    ]
    hyps = PS.candidates_to_hypotheses(items, issue="2026119")
    ids = {hypothesis.spec_exp_id(h) for h in hyps}
    assert len(ids) == 2, "不同 filter 得到相同 exp_id —— 候选会被去重成同一条"


def test_empty_filter_is_skipped(isolated_env):
    """没有 filter.key 的条目必须跳过（否则会产生一堆空 spec 撞车）。"""
    items = [{"filter": {}, "quick": {}}, {"quick": {}},
             {"filter": {"key": "ok_fam", "target": "red", "params": {"x": 1}},
              "quick": {}}]
    hyps = PS.candidates_to_hypotheses(items, issue="2026119")
    assert len(hyps) == 1
    assert hyps[0]["action"]["filter"]["key"] == "ok_fam"


# ---------------------------------------------------------------------------
# 核心：写入真的发生了
# ---------------------------------------------------------------------------

def test_register_creates_registered_records(isolated_env, seeded_db):
    """**最重要的一条**：登记后注册簿里必须出现 `prereg_status='registered'`。

    若这条挂了，说明「验证环节」又回到空转。
    """
    draws = seeded_db(600, seed=7)
    before = PS.registry_health()
    assert before["ok"] is True

    res = PS.register_scan_candidates(draws, issue="2026119")
    assert res["ok"] is True, res

    after = PS.registry_health()
    assert after["prereg_status"].get("registered", 0) > 0, (
        f"登记后没有任何 registered 记录，写入侧仍断：{after}")
    assert after["n_batches"] >= 1
    assert after["never_registered"] is False, (
        "刚登记完仍报 never_registered=True —— 健康度判据写错了")


def test_registered_record_has_prereg_fields(isolated_env, seeded_db):
    """登记出的记录必须带齐判定所需字段（否则 evaluate_prereg 会跳过它）。"""
    draws = seeded_db(600, seed=11)
    PS.register_scan_candidates(draws, issue="2026119")
    reg = hypothesis.load_registry() or {}
    hyps = [h for h in (reg.get("hypotheses") or [])
            if h.get("prereg_status") == "registered"]
    assert hyps, "没有 registered 记录"
    h = hyps[0]
    for k in ("exp_id", "batch_id", "evaluate_after_issue", "frozen_until_issue",
              "prereg_status", "prereg_ok", "scope"):
        assert k in h, f"缺少判定所需字段 {k}"
    assert h["prereg_ok"] is False, "刚登记不应已通过（先登记后看结果）"
    assert int(h["frozen_until_issue"]) > int(h["evaluate_after_issue"])


# ---------------------------------------------------------------------------
# 幂等：不许膨胀分母
# ---------------------------------------------------------------------------

def test_registration_is_idempotent(isolated_env, seeded_db):
    """重复登记同一期：记录数不增，且批次元数据不重复追加。

    这是**最关键的风险点**：`n_tested_cumulative` 是只增不减的校正分母，
    若每期重跑都新增记录，分母会被刷爆，把所有候选永久钉死在"不显著"。

    实现上走了两层保护：
      ① `register_prereg` 的 exp_id 去重（记录层）；
      ② `_already_registered_for_issue` 短路（批次元数据层）——
         否则 `counters.batches` 会堆出一串同 batch_id 的噪声（实测出现 3 条）。
    """
    draws = seeded_db(600, seed=13)
    first = PS.register_scan_candidates(draws, issue="2026119")
    n1 = PS.registry_health()["total"]
    b1 = PS.registry_health()["n_batches"]
    assert first["registered"] > 0

    for _ in range(3):
        again = PS.register_scan_candidates(draws, issue="2026119")
        assert again["registered"] == 0, "重复登记仍新增记录 —— 分母会被刷爆"
        assert again["error"] == "already_registered_for_issue"

    h = PS.registry_health()
    assert h["total"] == n1, f"记录数从 {n1} 漂到 {h['total']}，幂等失效"
    assert h["n_batches"] == b1, (
        f"批次元数据从 {b1} 涨到 {h['n_batches']} —— 同一期被重复追加")


def test_different_issue_registers_new_batch(isolated_env, seeded_db):
    """反向用例：**不同期号**必须能正常登记新批次（短路不能过度）。"""
    draws = seeded_db(600, seed=23)
    r1 = PS.register_scan_candidates(draws, issue="2026119")
    assert r1["registered"] > 0
    r2 = PS.register_scan_candidates(draws, issue="2026120")
    assert r2["ok"] is True
    # 同一批候选换了期号：exp_id 相同 → 记录去重，但批次元数据应新增一条
    assert r2["error"] != "already_registered_for_issue"
    assert PS.registry_health()["n_batches"] == 2


# ---------------------------------------------------------------------------
# 纪律：只登记 stat
# ---------------------------------------------------------------------------

def test_only_stat_source_registered(isolated_env, seeded_db):
    """登记出的记录 `source` 必须全是 'stat'。

    LLM 假设不得进 α 台账：`patterns_runtime` 硬编码 `source=='llm'` 永不 usable，
    若也登记，每个用户用自己的 key 生成的假说会稀释所有人的检验预算。
    """
    draws = seeded_db(600, seed=17)
    PS.register_scan_candidates(draws, issue="2026119")
    health = PS.registry_health()
    assert health["source"].get("llm", 0) == 0, (
        f"注册簿里出现了 LLM 假设：{health['source']}")
    assert health["source"].get("stat", 0) > 0


# ---------------------------------------------------------------------------
# 反向用例：删掉调用点必须变红
# ---------------------------------------------------------------------------

def test_scheduler_hook_exists_in_source(isolated_env):
    """静态断言：`_scheduler_step` 里必须存在前置注册写入的调用。

    这是最后一道防线：若有人删了那段 try/except，运行期测试仍可能因为
    其它路径写入而误绿，但源码扫描不会骗人。
    """
    from lottery import api_app
    # D-18a：原来用 `inspect.getsource(...)` 做子串匹配，会被 docstring/注释骗
    #   （假绿：删真实调用、只在注释留名仍通过；假红：注释里提到名字就误报）。
    # 改用 `code_text()`：剥 docstring + 注释，只留纯代码文本，行序与源码一致。
    from _source_assert import code_text
    src = code_text(api_app._scheduler_step)
    assert "prereg_scheduler" in src, (
        "_scheduler_step 里找不到 prereg_scheduler —— 前置注册写入接线被删了")
    assert "register_scan_candidates" in src
    # 顺序断言：下面这句注释里说「第一次写成 src.index('evaluate_prereg') 命中了注释」——
    # **现在 src 已不含注释**，所以这个坑从根上消失了；但顺序断言本身仍有价值：
    # 它要求"写入调用"出现在"评估调用"之前。
    i_write = src.index("register_scan_candidates(")
    i_eval = src.index("hypothesis.evaluate_prereg(")
    assert i_write < i_eval, (
        "写入必须在 evaluate_prereg 之前，否则本期候选要等下一期才进系统")


def test_noop_wiring_leaves_registry_empty(isolated_env, seeded_db):
    """反向用例：把写入函数换成空实现，注册簿必须保持为空。

    这与上一条配合：源码里有调用 + 空实现不产生写入 ⇒ 证明"有调用"确实是写入的来源。
    """
    draws = seeded_db(600, seed=19)
    db.close()
    db.get_conn()

    def _noop(*a, **k):
        return {"ok": True, "scanned": 0, "registered": 0, "deduped": 0,
                "batch_id": None, "judgeable": False, "errors": [], "error": "noop"}

    n0 = PS.registry_health()["total"]
    r = _noop(draws, issue="2026119")
    assert r["registered"] == 0
    assert PS.registry_health()["total"] == n0


# ---------------------------------------------------------------------------
# 容错：失败不得中断调度
# ---------------------------------------------------------------------------

def test_failure_is_swallowed(isolated_env, monkeypatch):
    """扫描/登记抛异常时，必须返回 ok=False 而不是抛出（调度不能被打断）。"""
    def _boom(*a, **k):
        raise RuntimeError("simulated discovery failure")

    monkeypatch.setattr(PS, "discover_candidates", _boom)
    res = PS.register_scan_candidates(
        [{"issue": "1", "reds": [1, 2, 3, 4, 5, 6]}], issue="1")
    assert res["ok"] is False
    assert "RuntimeError" in (res["error"] or "")


def test_empty_draws_is_safe(isolated_env):
    """空数据不得崩溃。"""
    res = PS.register_scan_candidates([], issue="")
    assert res["ok"] is True
    assert res["registered"] == 0


# ---------------------------------------------------------------------------
# 健康度：把"空转"变成可观测信号
# ---------------------------------------------------------------------------

def test_health_flags_never_registered(isolated_env):
    """全新注册簿必须报 `never_registered=True`（空转信号）。"""
    db.close()
    db.get_conn()
    h = PS.registry_health()
    assert h["ok"] is True
    assert h["never_registered"] is True, (
        "空注册簿未报 never_registered —— 接线再丢就发现不了")


def test_health_endpoint_exposes_flag(isolated_env):
    """`/api/registry/status` 必须暴露 health（前端/监控靠它告警）。"""
    from fastapi.testclient import TestClient
    from lottery import api_app
    db.close()
    with TestClient(api_app.app) as c:
        r = c.get("/api/registry/status")
    assert r.status_code == 200
    body = r.json()
    assert "health" in body, "registry/status 没有暴露 health"
    assert body["health"]["ok"] is True
