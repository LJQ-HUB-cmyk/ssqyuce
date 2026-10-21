"""A-4：产品口径与幻觉防线。

## 这一组测试守的是「不造假」，不是「功能」

用户的原话是「要真实可靠，不能造假」。本项目历史上最容易造假的地方不是篡改数字，
而是**用词**与**透传**：

1. **一个 `usable` 两个意思**：`idle_mining.usable`（挖掘层的过阈候选）
   与 `patterns_runtime.usable`（生产快照的可用规律）同名不同义。
   两者并列展示会让人看到「20 条可用」与「0 条可用」同时存在 ——
   哪怕每个数字单独看都对，用户也会认定系统自相矛盾。
2. **LLM 幻觉透传**：`patterns_used` 原来只做 `str(x)`，而前端会把它渲染成
   「规律引用：…」。`usable=0` 时任何引用都是幻觉。
3. **禁用词**：后端 `routes_predict.py:100-110` 明令「不得叫可用规律」，
   前端 `index.html` 却用了这个词。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------- 1. patterns_used 白名单 ----------

def test_sanitize_drops_unknown_patterns():
    """不在白名单的引用必须被丢弃，不能透传。"""
    from lottery import engine

    r = engine._sanitize_patterns_used(["hot_number", "编造的规律"], known={"hot_number"})
    assert r["kept"] == ["hot_number"]
    assert r["dropped"] == ["编造的规律"]
    assert r["had_any"] is True


def test_sanitize_empty_whitelist_drops_everything():
    """**当前生产的真实情形**：usable=0 ⇒ 白名单为空 ⇒ 任何引用都是幻觉。

    这是本组最重要的一条：没有可用规律时，LLM 说"我用了规律 X"必然是假的，
    必须整条丢弃，而不是"宽松放行"。
    """
    from lottery import engine

    r = engine._sanitize_patterns_used(["hot", "cold", "whatever"], known=set())
    assert r["kept"] == [], "白名单为空时不得保留任何引用"
    assert len(r["dropped"]) == 3

    r2 = engine._sanitize_patterns_used(["hot"], known=None)
    assert r2["kept"] == [], "known=None 等同空白名单"


def test_sanitize_handles_messy_input():
    """LLM 可能返回字符串、带空白的项、或逗号分隔串。"""
    from lottery import engine

    assert engine._sanitize_patterns_used("a, b ，c", known={"a", "b", "c"})["kept"] == ["a", "b", "c"]
    assert engine._sanitize_patterns_used(["  a  "], known={"a"})["kept"] == ["a"]
    assert engine._sanitize_patterns_used(None, known={"a"}) == {"kept": [], "dropped": [], "had_any": False}
    assert engine._sanitize_patterns_used([], known={"a"})["had_any"] is False


def test_parse_tickets_response_filters_patterns():
    """端到端：解析 LLM 响应时，伪造的规律引用不进 `patterns_used`。"""
    from lottery import engine

    res = {"tickets": [{
        "reds": [1, 2, 3, 4, 5, 6], "blue": 7, "confidence": 60,
        "patterns_used": ["真实规律", "不存在的规律"],
        "reasoning": "test",
    }]}
    out = engine._parse_tickets_response(res, "m1", known_patterns={"真实规律"})
    assert len(out) == 1
    assert out[0]["patterns_used"] == ["真实规律"]
    assert out[0]["patterns_used_dropped"] == ["不存在的规律"]


def test_parse_tickets_response_still_validates_numbers():
    """白名单改造不能削弱原有的号码合法性校验。"""
    from lottery import engine

    bad = {
        "tickets": [
            {"reds": [1, 2, 3, 4, 5], "blue": 7},          # 只有 5 个红球
            {"reds": [1, 2, 3, 4, 5, 6], "blue": 17},      # 蓝球越界
            {"reds": [1, 2, 3, 4, 5, 34], "blue": 7},      # 红球越界
            {"reds": [1, 1, 2, 3, 4, 5], "blue": 7},       # 红球重复
        ]
    }
    assert engine._parse_tickets_response(bad, "m", known_patterns=set()) == []


# ---------- 2. usable 命名分离 ----------

def test_idle_mining_returns_rule_evidence_not_usable():
    """`idle_mining` 的权威字段名必须是 `rule_evidence`。"""
    from lottery import idle_mining as IM

    st = IM.status()
    assert "rule_evidence_count" in st, "缺权威字段 rule_evidence_count"
    assert "usable_count_deprecated" in st, "旧字段必须显式标注 deprecated"
    # 两个字段值相同（兼容期），但语义已由新名承载
    assert st["rule_evidence_count"] == st["usable_count_deprecated"]


def test_run_once_result_marks_usable_as_deprecated():
    """`run_once` 返回值保留 usable 别名时必须带弃用标记。"""
    src = (REPO / "lottery" / "idle_mining.py").read_text(encoding="utf-8")
    assert '"usable_deprecated": True' in src, \
        "兼容别名必须显式标记为弃用，否则会与 patterns_runtime.usable 混淆"


def test_rule_evidence_is_not_wired_into_prediction():
    """**架构守卫**：挖掘层的证据项不得进入出号门控。

    出号门控看 `patterns_runtime.counts.usable_replayable`。
    若哪天有人把 `rule_evidence` 接到 predict 上，就是绕过前置注册与 A/B 验证。
    """
    src = (REPO / "lottery" / "routes_predict.py").read_text(encoding="utf-8")
    assert "rule_evidence" not in src, \
        "rule_evidence 不应出现在出号门控里（它只是挖掘层的统计结论）"


# ---------- 3. 前端禁用词 ----------

def test_index_html_does_not_call_shape_rules_usable_law():
    """前端不得把 L2 结构约束叫「可用规律」。

    后端 `routes_predict.py:100-110` 明令禁用该词：
    「不得把它叫成『可用规律』——L1 的语义是检出/未检出，
    与 L2 的约束满足率不是同一种计数」。
    """
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    # 找出所有「可用规律」出现处，排除纯注释行（HTML 注释里的旧说明不改）
    offenders = []
    for i, line in enumerate(html.splitlines(), 1):
        if "可用规律" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("<!--") or stripped.endswith("-->"):
            continue          # HTML 注释，用户看不到
        if "不得" in line or "不是" in line or "禁止" in line:
            continue          # 显式否定句
        offenders.append((i, stripped[:100]))
    assert not offenders, f"index.html 仍把约束称作「可用规律」：{offenders}"


def test_app_js_metric_uses_replayable_field():
    """前端首屏指标必须读 `usable_replayable`，不能只读 `usable`。"""
    js = (REPO / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "usable_replayable" in js, \
        "前端未读 usable_replayable —— 会与挖掘层的 usable 混淆"


# ---------- 4. 后端纪律本身（防止有人放宽） ----------

def test_backend_discipline_text_intact():
    """后端「不得叫可用规律」的纪律文本必须还在。"""
    src = (REPO / "lottery" / "routes_predict.py").read_text(encoding="utf-8")
    assert "不得" in src and "可用规律" in src, \
        "后端口径纪律文本被删了 —— 那是防止口径漂移的唯一书面依据"
