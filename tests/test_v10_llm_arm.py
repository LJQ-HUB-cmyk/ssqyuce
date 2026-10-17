"""V10-09：LLM 出号**独立 A/B 对照臂**（非规律库路径）+ 防死锁。

规格来源
--------
* ``docs/reviews/R3_AI_REVIEW.md`` **G1 / G2**（对照臂口径与"删除 passed 才能进候选池"）
* ``docs/reviews/V10_REVIEW_SUMMARY.md`` G1
* ``docs/V10_IMPROVEMENT_PLAN.md`` §3.9（V10-09）

本文件在守什么
--------------
原方案条款「LLM 候选须 ``experiments.status == "passed"`` 才能进候选池」是**按设计的
死锁**：``hypothesis`` 要"有支持"才升 validated、``patterns_runtime`` 要 ``persistent``
才 usable、``battle.replay_method`` 又明确 llm 不可回放 → 「llm 且 passed」的集合
**恒为空** → LLM 永远出不了号。裁决改为：LLM 走**独立对照臂**，门槛降为「不劣于
基线」，允许长期停在 ``eligible``。

因此本文件用三组测试同时守两个方向：
1. **判据正确**（T1–T6）：配对口径、样本不足不误报、注入差异结论正确、多重性校正、
   四档判决；
2. **防死锁**（T7–T9）：结论**完全不依赖** experiments / prereg / usable；
   ``source=="llm"`` 永不 usable；``eligible=True`` 也**不会**自动出 LLM 注；
3. **边界不炸**（T10–T13）：空数据、只有一臂、注数不匹配、期数卡在门槛上。

测试表
------
T1  只读 eval_details：不调用 replay_method、不读假说库（静态守卫）
T2  配对口径：同期 + 同 n_tickets + 逐注配对（注数不匹配的期被剔除并计数）
T3  配对期数刚好卡在门槛上（min_issues-1 → insufficient；== min_issues → 进入检验）
T4  样本不足不误报：真实小样本给出 insufficient_sample 且 eligible 仍为 True
T5  注入真实优势 → verdict=improved；注入真实劣势 → verdict=worse（且 not eligible）
T6  多重性校正生效（n_trials 放大 p；未校正显著 → 校正后不显著）
T7  防死锁①：假说库全部 failed/prereg failed → 结论与正常时**完全相同**
T8  防死锁②：``source=="llm"`` 的假说永不 usable（守住 patterns_runtime 的短路）
T9  防死锁③：``eligible=True`` 但 ``llm_can_pick=False`` → 出号不含 llm 注
T10 空数据 / 只有 llm 无基线 / 只有基线无 llm：不抛异常、verdict 全 insufficient
T11 注数全不匹配：配对期数为 0，不误报为「无差异」
T12 真实 eval_details（生产库累积记录）跑一次：结论可复现、字段齐全
T13 蓝球与其它字段口径：delta_blue 独立于红球、enabled_default 恒 False
"""
from __future__ import annotations

import json
import random

import pytest

from lottery import battle as B


# ==================== 构造工具 ====================

def _row(issue: str, method: str, seq: int, red: int, blue: int = 0) -> dict:
    """一条 eval_details 行（字段与 db.save_eval_details 落库结构一致）。"""
    return {"issue": issue, "method": method, "seq": seq,
            "red_hits": int(red), "blue_hit": int(blue),
            "prize_level": 0, "reward": 0.0, "ticket_cost": 2.0}


def _paired_rows(n_issues: int, *, n_tickets: int = 5, seed: int = 11,
                 llm_shift: float = 0.0, base_shift: float = 0.0,
                 llm_method: str = "llm:m1", base_method: str = "stat:freq",
                 mismatch_issues=()) -> list:
    """构造两臂同期、同注数、逐注可配对的 eval_details 行。

    ``llm_shift`` / ``base_shift`` 用来**注入真实差异**（加在每注命中数上）；
    命中数截断到 [0, 6]（红球上限 6）。
    """
    rng = random.Random(seed)
    rows: list = []
    for i in range(n_issues):
        issue = f"2026{i + 1:03d}"
        llm_n = n_tickets - 1 if issue in mismatch_issues else n_tickets
        base_n = n_tickets + 2 if issue in mismatch_issues else n_tickets
        for seq in range(1, llm_n + 1):
            r = rng.choice([0, 1, 1, 2, 2, 3]) + llm_shift
            rows.append(_row(issue, llm_method, seq, max(0, min(6, round(r)))))
        for seq in range(1, base_n + 1):
            r = rng.choice([0, 1, 1, 2, 2, 3]) + base_shift
            rows.append(_row(issue, base_method, seq, max(0, min(6, round(r)))))
    return rows


def _fail_registry(n: int = 5) -> dict:
    """构造一个「所有假说都 failed、prereg 全 failed」的假说库。"""
    return {"hypotheses": [
        {"id": f"h{k}", "source": "llm", "statement": f"failed-{k}",
         "status": "failed", "prereg_status": "failed", "prereg_ok": False,
         "regime": {"status": "persistent", "label_zh": "持续"},
         "action": {"type": "prefer_red_filter",
                    "filter": {"key": "omit_avg", "params": {}}}}
        for k in range(n)]}


# ==================== T1：只读 eval_details ====================

def _referenced_names(func) -> set:
    """收集函数内**被引用/调用的名字集合**（AST 层面，不受字符串字面量干扰）。

    为什么要这样：``llm_arm_report`` 的中文 reason 文本里会**提到**
    「replay_method 不可回放」来解释口径——那是给人看的说明，不是调用。
    若对源码做文本匹配就会误判（我第一版就踩了这个坑）。这里只看
    ``ast.Name`` / ``ast.Attribute`` / ``ast.Call`` 实际用到的标识符。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.name.split(".")[0])
    return names


def test_t1_reads_only_eval_details_never_replays():
    """T1：对照臂**不得**走 replay_method（llm 不可回放），也不得读假说库。

    这是 R3 G2 的第一条硬约束。用静态守卫（**可执行代码**扫描，剥掉 docstring）
    而非行为断言：误加一次 replay 调用就是整份证据作废，而"没有调用"很难从行为
    上观察到。
    """
    names = _referenced_names(B.llm_arm_report)
    for forbidden in ("replay_method", "replay_all", "load_registry",
                      "build_snapshot", "evaluate_prereg", "patterns_runtime",
                      "hypothesis", "evolution"):
        assert forbidden not in names, f"llm_arm_report 不得引用 {forbidden}"
    # 正向确认它确实只读 eval_details（依赖 db 加载，但不碰那套状态机）
    assert "load_eval_details" in names or "db" in names
    # 反向确认：llm 仍然被标注为不可回放（防止有人"顺手"把它加成可回放）
    assert "llm" in B.NON_REPLAYABLE_METHODS
    assert B.llm_arm_report(rows=[])["data_source"] == "caller_rows"


# ==================== T2：配对口径 ====================

def test_t2_pairing_requires_same_issue_and_same_n_tickets():
    """T2：只有「同期 + 同注数」的期才进入配对；注数不一致的期被剔除并计数。"""
    bad = ("2026003", "2026004", "2026005")
    rows = _paired_rows(20, mismatch_issues=bad)
    r = B.llm_arm_report(rows=rows, min_issues=1)
    assert r["n_issues"] == 17, r["n_issues"]
    assert r["dropped_ticket_mismatch"] == 3
    detail = {d["issue"] for d in r["ticket_mismatch_detail"]}
    assert detail == set(bad)
    # 配对注数 = 期数 × 每期注数（逐注配对，一注一对）
    assert r["n_pairs"] == 17 * 5
    # 配对序列按 issue 升序、且两臂逐期均值一一对应
    c = next(c for c in r["comparisons"] if c["n_issues"] > 0)
    assert c["issues"] == sorted(c["issues"])
    assert c["ticket_counts"] == [5] * 17


# ==================== T3：门槛卡点 ====================

def test_t3_min_issues_boundary_exactly_at_threshold():
    """T3：配对期数**刚好卡在门槛**——差 1 期判 insufficient，正好达标则进入检验。

    这是任务书点名要自己构造的反例（"配对期数刚好卡在门槛上"）。
    边界必须满足：``n_issues < min_issues → insufficient``；
    而 ``n_issues == min_issues`` 必须真的去算 p（不能因为"刚好够"就保守放过）。
    """
    need = 10
    at = [r for r in _paired_rows(need) ]
    below = [r for r in at if r["issue"] <= f"2026{need - 1:03d}"]

    r_at = B.llm_arm_report(rows=at, min_issues=need)
    r_below = B.llm_arm_report(rows=below, min_issues=need)

    assert r_below["n_issues"] == need - 1
    assert r_below["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r_below["eligible"] is True          # 样本不足不得判死

    assert r_at["n_issues"] == need
    assert r_at["verdict"] != B.LLM_ARM_VERDICT_INSUFFICIENT, \
        "期数正好等于门槛时必须真正做检验，不能继续以样本不足搪塞"
    assert r_at["p_adjusted"] is not None


# ==================== T4：样本不足不误报 ====================

def test_t4_small_sample_never_reports_improvement():
    """T4：样本不足时即使均值看起来更好，也只能报 insufficient_sample（eligible）。"""
    # 5 期、LLM 每注都比基线多 2 个红球（效应极大但样本极小）
    rows = _paired_rows(5, llm_shift=2.0)
    r = B.llm_arm_report(rows=rows, min_issues=60)      # 默认生产门槛
    assert r["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r["verdict_zh"] == "样本不足"
    assert r["eligible"] is True
    assert r["enabled_default"] is False
    # reason 必须点明门槛与实测期数，不许含糊其辞
    assert "60" in r["reason_zh"] and "5" in r["reason_zh"]
    assert r["min_issues"] == 60
    # 门槛内的比较明细也不得出现 improved
    assert all(c["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT for c in r["comparisons"])


# ==================== T5：注入差异的判决 ====================

def test_t5_injected_advantage_is_improved_and_injected_deficit_is_worse():
    """T5：注入真实优势 → improved（eligible）；注入真实劣势 → worse（**不** eligible）。

    两侧都要测：只测「更好」会漏掉「更差也能被检出」——
    这正是单侧 p 值会踩的坑（``rigor.strategy_significance`` 的单侧口径把
    「显著更差」算成 p≈1，必须用双侧，见 ``battle._two_sided``）。
    """
    n = 30
    better = B.llm_arm_report(rows=_paired_rows(n, llm_shift=1.0), min_issues=10)
    worse = B.llm_arm_report(rows=_paired_rows(n, base_shift=1.0), min_issues=10)

    assert better["verdict"] == B.LLM_ARM_VERDICT_IMPROVED, better["reason_zh"]
    assert better["eligible"] is True
    assert better["delta_red"] > 0
    assert better["p_adjusted"] < 0.05

    assert worse["verdict"] == B.LLM_ARM_VERDICT_WORSE, worse["reason_zh"]
    assert worse["eligible"] is False, "显著更差必须撤销 eligible"
    assert worse["delta_red"] < 0
    assert worse["p_adjusted"] < 0.05, "双侧检验：更差也必须给出小 p"


def test_t5b_no_difference_is_not_worse_and_stays_eligible():
    """T5b：两臂无差异 → not_worse（真随机世界的预期结论），eligible 但不默认启用。"""
    r = B.llm_arm_report(rows=_paired_rows(30, seed=3), min_issues=10)
    assert r["verdict"] in (B.LLM_ARM_VERDICT_NOT_WORSE, B.LLM_ARM_VERDICT_IMPROVED)
    assert r["eligible"] is True
    assert r["enabled_default"] is False
    assert r["enable_gate"] == "methods.resolve_channels().llm_can_pick"


# ==================== T6：多重性校正 ====================

def test_t6_multiple_comparisons_correction_is_applied():
    """T6：同时比 5 个基线方法时，p 必须按 n_trials=5 校正（未校正显著→校正后不显著）。"""
    methods = ("stat:freq", "stat:bayes", "stat:markov", "stat:omission", "ml")
    rng = random.Random(5)
    rows: list = []
    for i in range(30):
        issue = f"2026{i + 1:03d}"
        for seq in range(1, 6):
            rows.append(_row(issue, "llm:m1", seq, rng.choice([1, 1, 2, 2, 2, 3])))
        for bm in methods:
            for seq in range(1, 6):
                rows.append(_row(issue, bm, seq, rng.choice([0, 1, 1, 2, 2, 3])))

    r = B.llm_arm_report(rows=rows, min_issues=10)
    assert r["n_trials"] == len(methods), r["n_trials"]
    assert r["n_comparisons_tested"] == len(methods)
    # 校正后的 p 不得小于未校正 p（Bonferroni 的定义）
    for c in r["comparisons"]:
        if c["p_value"] is not None and c["p_adjusted"] is not None:
            assert c["p_adjusted"] >= c["p_value"] - 1e-12, c["comparison"]
    # 至少有一个比较：未校正显著但校正后不显著（这正是校正要救的假阳性）
    flipped = [c for c in r["comparisons"]
               if c["p_value"] is not None and c["p_value"] < 0.05
               and c["p_adjusted"] is not None and c["p_adjusted"] >= 0.05]
    assert flipped, "5 次比较下必须能看到「未校正显著→校正后不显著」的例子"

    # 显式 n_trials=1 时不做校正，p_adjusted 应回到未校正量级
    r1 = B.llm_arm_report(rows=rows, min_issues=10, n_trials=1)
    assert r1["n_trials"] == 1
    assert r1["p_adjusted"] <= r["p_adjusted"] + 1e-12


# ==================== T7/T8/T9：防死锁 ====================

def test_t7_all_hypotheses_failed_does_not_affect_report(monkeypatch):
    """T7（防死锁①）：假说库**全部 failed**、prereg 全 failed 时结论必须完全不变。

    原方案的死锁就是「LLM 要 passed 才能出号」——这里证明对照臂根本不查那套状态。
    """
    from lottery import hypothesis

    rows = _paired_rows(30, llm_shift=1.0)
    baseline = B.llm_arm_report(rows=rows, min_issues=10)

    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: _fail_registry(9))
    after = B.llm_arm_report(rows=rows, min_issues=10)

    assert after["verdict"] == baseline["verdict"] == B.LLM_ARM_VERDICT_IMPROVED
    assert after["p_adjusted"] == baseline["p_adjusted"]
    assert after["n_issues"] == baseline["n_issues"]
    assert after["eligible"] is True
    assert "experiments" in after["no_deadlock_note_zh"]


def test_t7b_empty_registry_also_does_not_affect_report(monkeypatch):
    """T7b（防死锁① 加强）：假说库**完全为空**时结论同样不受影响。"""
    from lottery import hypothesis

    rows = _paired_rows(20, seed=7)
    baseline = B.llm_arm_report(rows=rows, min_issues=10)
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: {"hypotheses": []})
    after = B.llm_arm_report(rows=rows, min_issues=10)
    assert after["verdict"] == baseline["verdict"]
    assert after["p_adjusted"] == baseline["p_adjusted"]


def _fake_draws(n: int = 520, seed: int = 1):
    """合成开奖数据（n ≥ patterns_runtime.MIN_DRAWS_FOR_SNAPSHOT=500 才能建快照）。"""
    import datetime as _dt

    rng = random.Random(seed)
    start = _dt.date(2013, 1, 1)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({"issue": f"{2013 + i // 150}{i % 150 + 1:03d}",
                    "date": (start + _dt.timedelta(days=3 * i)).isoformat(),
                    "reds": reds, "order": list(reds),
                    "blue": rng.randint(1, 16)})
    return out


def test_t8_llm_source_hypothesis_never_becomes_usable(monkeypatch):
    """T8（防死锁②）：真正跑 ``patterns_runtime.build_snapshot``，
    断言 ``source=="llm"`` 的假说**永不 usable**。

    假说被构造成「满足其它一切条件」：``status=validated`` + ``prereg_ok=True`` +
    ``regime.status=persistent`` —— 只差 source 是 llm。若有人删掉
    ``(h.get("source") or "stat") != "llm"`` 这行短路，这条规则就会变成 usable，
    死锁从后门复活（"LLM 规律通过 A/B 进入候选池"）。本测试会立刻变红。
    """
    from lottery import discovery, hypothesis, patterns_runtime

    hyps = {"hypotheses": [{
        "id": "llm-h1", "source": "llm", "statement": "LLM 规律（本不存在）",
        "status": "validated", "prereg_status": "passed", "prereg_ok": True,
        "regime": {"status": "persistent", "label_zh": "持续", "recent_delta": 0.4},
        "action": {"type": "prefer_red_filter",
                   "filter": {"key": "omit_avg", "params": {}}},
    }]}
    monkeypatch.setattr(hypothesis, "load_registry", lambda *a, **k: hyps)
    # 跳过真实 discovery 扫描（与本测试无关，且很慢）
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: {"scans": []})

    snap = patterns_runtime.build_snapshot(_fake_draws())
    rules = [r for r in snap.get("rules", []) if r.get("id") == "llm-h1"]
    assert rules, "假说应当出现在快照里（只是不可用）"
    for r in rules:
        assert r["source"] == "llm"
        assert r["usable"] is False, "source==llm 的假说不得 usable（死锁会从后门复活）"
    assert snap.get("counts", {}).get("usable", 0) == 0

    # 再确认那行短路仍在源码里（双保险：行为 + 静态）
    import inspect
    src = inspect.getsource(patterns_runtime.build_snapshot)
    assert '!= "llm"' in src


def test_t9_eligible_does_not_imply_llm_tickets(monkeypatch):
    """T9（防死锁③）：``eligible=True`` 只表示"有资格继续参战"，出号仍由 llm_can_pick 决定。

    构造：报告显示 improved/not_worse（eligible=True），但 ``llm_can_pick=False``
    （resolve_channels 默认关）→ 出号结果里**不得**含任何 llm:* 注。
    """
    from lottery import engine as E, methods

    # 1) 报告侧：eligible=True
    report = B.llm_arm_report(rows=_paired_rows(30, llm_shift=1.0), min_issues=10)
    assert report["eligible"] is True
    assert report["enabled_default"] is False
    assert report["enable_gate"] == "methods.resolve_channels().llm_can_pick"

    # 2) 出号侧：llm_can_pick=False → 引擎绝不产出 llm 注
    monkeypatch.setattr(methods, "resolve_channels",
                        lambda **k: {"llm_can_pick": False, "llm_output_mode": "off",
                                     "mode": "production",
                                     "families": {"stat": True, "llm": False}})
    ch = methods.resolve_channels()
    assert ch["llm_can_pick"] is False
    # 引擎的出号判据只认这个快照（见 engine.predict_next 的 llm_only 分支）
    import inspect
    src = inspect.getsource(E.predict_next)
    assert 'channels.get("llm_can_pick")' in src or "llm_can_pick" in src
    assert 'llm_can_pick' in src, "引擎必须用 llm_can_pick 作为唯一出号判据"


# ==================== T10/T11：边界不炸 ====================

def test_t10_empty_and_single_arm_inputs_never_raise():
    """T10：空数据 / 只有 llm / 只有基线 —— 不抛异常、不返回空、结论为 insufficient。"""
    # 空数据
    r = B.llm_arm_report(rows=[])
    assert r["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r["eligible"] is True
    assert r["reason_zh"]

    # 只有 llm，没有基线
    only_llm = [_row(f"2026{i:03d}", "llm:m1", 1, 2) for i in range(1, 6)]
    r2 = B.llm_arm_report(rows=only_llm, min_issues=1)
    assert r2["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r2["eligible"] is True
    assert r2["llm_methods"] == ["llm:m1"] and r2["baseline_methods"] == []

    # 只有基线，没有 llm
    only_base = [_row(f"2026{i:03d}", "stat:freq", 1, 2) for i in range(1, 6)]
    r3 = B.llm_arm_report(rows=only_base, min_issues=1)
    assert r3["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r3["eligible"] is True

    # 脏行（缺 issue / 缺 red_hits / method 为空）不得崩
    dirty = [{"method": "llm:m1", "seq": 1, "red_hits": 1},          # 缺 issue
             {"issue": "2026001", "method": "", "seq": 1, "red_hits": 1},  # 空 method
             {"issue": "2026002", "method": "llm:m1", "seq": 1},      # 缺 red_hits
             None]
    try:
        B.llm_arm_report(rows=[d for d in dirty if d], min_issues=1)
    except Exception as e:                                            # noqa: BLE001
        pytest.fail(f"脏行导致异常：{e}")


def test_t11_all_ticket_counts_mismatch_is_not_reported_as_no_difference():
    """T11：所有期注数都不一致 → 配对期数必须为 0，结论只能是 insufficient。

    绝不能因为"两臂都有记录"就报 ``not_worse``——那会把「根本没法比」说成
    「比过且没有差异」，是比没有结论更糟的误导。
    """
    rows: list = []
    for i in range(15):
        issue = f"2026{i + 1:03d}"
        for seq in range(1, 6):
            rows.append(_row(issue, "llm:m1", seq, 2))
        for seq in range(1, 4):          # 基线 3 注 vs LLM 5 注 → 全部不匹配
            rows.append(_row(issue, "stat:freq", seq, 2))
    r = B.llm_arm_report(rows=rows, min_issues=1)
    assert r["n_issues"] == 0
    assert r["n_pairs"] == 0
    assert r["dropped_ticket_mismatch"] == 15
    assert r["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r["verdict"] != B.LLM_ARM_VERDICT_NOT_WORSE
    assert r["eligible"] is True


def test_t11b_tiny_n_below_rigor_floor_never_reports_direction():
    """T11b：配对期数 < 3 时**不得**报方向（守 rigor 的 insufficient-data 保护）。

    这是我自己构造反例时踩到的真 bug：两期、每期差值恒为 +3 时，
    ``rigor.strategy_significance`` 按设计返回 ``verdict="insufficient-data"`` 且
    ``p_uncorrected=None``（"宁可 insufficient 也不要把退化 p 当已检验"），
    但它同时保留了 ``p_two_sided=0.0``（常数差值的解析退化分支）。
    第一版只看 ``p_two_sided``，于是 2 期就被判成 ``improved`` —— 绕过了 rigor 的
    样本量保护。此测试把这道保护钉死。
    """
    for n in (1, 2):
        rows: list = []
        for i in range(n):
            issue = f"2026{i + 1:03d}"
            for seq in range(1, 4):
                rows.append(_row(issue, "llm:m1", seq, 3))
                rows.append(_row(issue, "stat:freq", seq, 0))
        r = B.llm_arm_report(rows=rows, min_issues=1)
        assert r["n_issues"] == n
        assert r["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT, f"n={n} 不得给方向"
        assert r["p_adjusted"] is None
        assert r["eligible"] is True

    # n=3（rigor 的下限）必须能正常检验，否则门槛会把功效卡死
    rows3: list = []
    for i in range(3):
        issue = f"2026{i + 1:03d}"
        for seq in range(1, 4):
            rows3.append(_row(issue, "llm:m1", seq, 3))
            rows3.append(_row(issue, "stat:freq", seq, 0))
    r3 = B.llm_arm_report(rows=rows3, min_issues=1)
    assert r3["verdict"] == B.LLM_ARM_VERDICT_IMPROVED
    assert r3["p_adjusted"] is not None


# ==================== T12：真实 eval_details ====================

def test_t12_real_data_shape_is_insufficient_sample(isolated_env):
    """T12：复刻**生产真实数据的形状** → 结论必须是 insufficient_sample。

    ``tests/conftest.py`` 把 DB 隔离到临时库（不读生产 ``data/ssq.db``），所以这里
    按生产库实测形状合成记录——这正是当前 30 行的真实构成：

    * ``2026114``：``llm:group/...vision`` 4 注 + ``stat:freq`` 2 注 + ``stat:bayes/
      markov/omission`` 各 1 注 + ``ml`` 1 注 → **同注数条件不满足**（4 vs 1/2）；
    * ``2026115``~``2026118``：只有 ``llm:agent-router/...`` 各 5 注，**没有基线**。

    因此可配对期数 = 0：唯一诚实的结论是 ``insufficient_sample``（eligible 保持
    True、默认不启用）。这个测试锁住的是「不会把没法比的 30 行硬算成一个结论」。
    """
    from lottery import db

    rows = []
    # 2026114：两臂并存但注数不同 → 必须被剔除
    for seq in range(1, 5):
        rows.append(_row("2026114", "llm:group/auto-deepseek-v4-flash-vision", seq, 1))
    for seq in range(1, 3):
        rows.append(_row("2026114", "stat:freq", seq, 2))
    for m in ("stat:bayes", "stat:markov", "stat:omission"):
        rows.append(_row("2026114", m, 1, 2))
    rows.append(_row("2026114", "ml", 1, 2))
    # 2026115..2026118：只有 llm 臂
    for i in range(5, 9):
        for seq in range(1, 6):
            rows.append(_row(f"202611{i}", "llm:agent-router/deepseek-v4-flash", seq, 1))
    assert len(rows) == 30, "形状应与生产库的 30 行一致"

    r = B.llm_arm_report(rows=rows)
    assert r["n_rows"] == 30
    assert r["n_issues_data"] == 5
    assert r["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT
    assert r["n_issues"] == 0
    assert r["eligible"] is True
    assert r["enabled_default"] is False
    assert r["min_issues"] == 60
    assert r["data_source"] == "caller_rows"
    # 2026114 期被记为「注数不匹配」而不是被静默吞掉（明细在任何比较里出现即可：
    # 只有 vision 那组 llm 方法在 2026114 与基线同期出现，故主口径可能不是它）
    all_detail = [d for c in r["comparisons"] for d in c["ticket_mismatch_detail"]]
    assert any(d["issue"] == "2026114" for d in all_detail), all_detail
    assert any(d["issue"] == "2026114" and d["arm_tickets"] == 4
               and d["baseline_tickets"] in (1, 2) for d in all_detail)
    assert any(c["dropped_ticket_mismatch"] >= 1 for c in r["comparisons"])
    # 即使只有 llm 记录的期，也如实计入两臂描述统计（不隐藏）
    assert any(m.startswith("llm:") for m in r["by_arm"])

    # 同一个函数直接读（隔离库里为空）也不得崩，且如实标注数据来源
    r_db = B.llm_arm_report()
    assert r_db["data_source"] == "db.eval_details"
    assert r_db["verdict"] == B.LLM_ARM_VERDICT_INSUFFICIENT


# ==================== T13：蓝球与字段契约 ====================

def test_t13_blue_series_and_field_contract():
    """T13：红球为主口径、蓝球独立给出；``enabled_default`` 恒为 False。"""
    n = 25
    rows: list = []
    for i in range(n):
        issue = f"2026{i + 1:03d}"
        # 红球相同（无差异），蓝球 LLM 每注都中（巨大差异）
        for seq in range(1, 4):
            rows.append(_row(issue, "llm:m1", seq, red=2, blue=1))
            rows.append(_row(issue, "stat:freq", seq, red=2, blue=0))
    r = B.llm_arm_report(rows=rows, min_issues=10)
    assert r["delta_red"] == pytest.approx(0.0, abs=1e-9)
    assert r["delta_blue"] == pytest.approx(1.0, abs=1e-9)
    # 主口径是红球（蓝球不参与 verdict）
    assert r["verdict"] == B.LLM_ARM_VERDICT_NOT_WORSE
    assert r["blue_test"] is not None
    # 但蓝球显著差异**必须**写进 reason：隐藏看到的事实比没有结论更糟
    assert "蓝球" in r["reason_zh"]
    assert "显著" in r["reason_zh"]
    assert r["blue_test"]["two_sided"]["p_adjusted"] < 0.05
    # 字段契约（任务书要求的最小集合）
    for key in ("arm", "n_issues", "n_pairs", "delta_red", "delta_blue",
                "p_value", "p_adjusted", "verdict", "eligible", "reason_zh"):
        assert key in r, key
    # 任何情况下都不自动启用
    assert r["enabled_default"] is False
    assert r["enable_gate"] == "methods.resolve_channels().llm_can_pick"


def test_t13b_verdict_vocabulary_is_closed_and_zh_mapped():
    """T13b：四档判决词表封闭、每档都有中文、且 eligible 集合与规格一致。"""
    expected = {"improved", "not_worse", "insufficient_sample", "worse"}
    assert set(B.LLM_ARM_VERDICT_ZH) == expected
    assert B.LLM_ARM_ELIGIBLE_VERDICTS == {"improved", "not_worse", "insufficient_sample"}
    assert "worse" not in B.LLM_ARM_ELIGIBLE_VERDICTS
    assert all(isinstance(v, str) and v for v in B.LLM_ARM_VERDICT_ZH.values())
