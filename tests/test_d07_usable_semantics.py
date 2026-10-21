"""D-07：`usable` 一词两义的语义标注与「禁止并列展示」守卫。

## 缺陷背景

同一系统里有两个 `usable`，数值不同：

```
data/idle_mining_state.json : by_decision.usable = 20   ← 单候选过阈（未校正）
data/patterns_runtime.json  : usable = 0                 ← 生产快照（可回放）
```

两者**都对**，但用户看到「20 条可用」与「0 条可用」并列就会认定系统造假。

## 本轮范围（刻意克制）

* **只加 `usable_semantics` 标注 + 加守卫禁止并列展示**；
* **不删** `idle_mining` 的 `usable` 弃用别名（改名 `e_reject_candidates` 留给下一版本）；
* **不改** `_group_checked` 的 m 记账（那是 D-08，且修完 `usable` 仍为 0）。

## 本文件守什么

1. 标注存在，且**明确**写出两个字段同名不同义；
2. 权威字段是 `rule_evidence_count`，不是 `usable`；
3. **任何单一响应体里不得同时出现两个 usable 值** —— 靠扫描代码中
   「同时构造两个字段」的路径来证明，而不是靠文档承诺。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from lottery import idle_mining as IM


ROOT = pathlib.Path(__file__).resolve().parents[1]


# ==========================================================================
# 一、标注本身
# ==========================================================================

def test_status_carries_usable_semantics():
    """`status()` 必须携带 `usable_semantics` 段落。"""
    st = IM.status()
    assert "usable_semantics" in st, "缺少 usable_semantics 标注"
    sem = st["usable_semantics"]
    assert sem.get("this_module_authoritative_field") == "rule_evidence_count"
    assert sem.get("never_show_together") is True


def test_semantics_names_both_sides():
    """标注必须同时说明「本侧」与「对侧」的含义，只说一半没用。"""
    sem = IM.status()["usable_semantics"]
    assert sem.get("meaning_zh"), "缺本侧含义"
    collide = sem.get("collides_with") or {}
    assert collide.get("field") == "patterns_runtime.counts.usable", \
        "必须点名撞词的字段"
    assert collide.get("meaning_zh"), "缺对侧含义"


def test_semantics_declares_deprecation_and_rename_plan():
    """旧名必须标为弃用，并写明下一版本的改名目标。"""
    sem = IM.status()["usable_semantics"]
    aliases = sem.get("deprecated_aliases") or []
    assert "usable_count_deprecated" in aliases
    assert "e_reject_candidates" in (sem.get("rename_plan") or ""), \
        "必须写明下一版本改名为 e_reject_candidates"


def test_authoritative_field_present_and_preferred():
    """权威字段必须存在；`usable` 别名若存在也不得冒充权威。"""
    st = IM.status()
    assert "rule_evidence_count" in st, "缺少权威字段 rule_evidence_count"
    sem = st["usable_semantics"]
    assert sem["this_module_authoritative_field"] == "rule_evidence_count"


# ==========================================================================
# 二、【核心】禁止并列展示 —— 用代码扫描证明，不靠文档承诺
# ==========================================================================

def _route_modules():
    return sorted(ROOT.glob("lottery/routes_*.py")) + [ROOT / "lottery/api_app.py"]


def test_no_route_returns_both_usable_fields():
    """扫描所有路由模块：不得有任何一处**同时**构造「挖掘层 usable」与
    「生产快照 usable」两个键。

    做法（AST 而非正则，避开注释与字符串误报）：
    找出每个 dict 字面量的键集合，若某个 dict 同时含 `usable`（或
    `rule_evidence_count`）与 `usable_replayable`，则视为并列展示路径。

    为什么这是真守卫：用户之所以认定造假，是因为**在同一个响应里**看到了
    两个打架的数字。跨端点各说各的（且已有语义标注）不算违规。
    """
    offenders = []
    for path in _route_modules():
        if not path.exists():
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = set()
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
            mining_side = bool(keys & {"usable", "usable_count",
                                       "usable_count_deprecated",
                                       "rule_evidence_count"})
            runtime_side = "usable_replayable" in keys
            if mining_side and runtime_side:
                offenders.append(f"{path.name}:{node.lineno} keys={sorted(keys)}")
    assert not offenders, (
        "发现同时返回两个 usable 口径的路径（并列展示会让用户认定造假）：\n"
        + "\n".join(offenders))


def test_no_frontend_reads_both_usable_fields():
    """前端不得在同一处读取两个 usable 口径。

    前端是用户实际看到的唯一出口 —— 后端再干净，前端拼在一起也一样。
    这里用文本扫描（前端是 JS，无 AST 可依），只查**同一行**同时出现两侧字段。
    """
    app_js = ROOT / "web/static/app.js"
    if not app_js.exists():
        pytest.skip("前端文件不存在")
    bad = []
    for i, line in enumerate(app_js.read_text(errors="ignore").splitlines(), 1):
        mining = ("usable_count" in line or "by_decision.usable" in line
                  or "rule_evidence_count" in line)
        runtime = "usable_replayable" in line
        if mining and runtime:
            bad.append(f"app.js:{i}: {line.strip()[:120]}")
    assert not bad, "前端同一行同时引用两侧 usable 口径：\n" + "\n".join(bad)


def test_annotation_keys_do_not_drift():
    """标注必须随 `status()` 一起演进：键名不得漂移。

    （防止有人「顺手重命名」掉标注，让守卫静默失效。）
    """
    sem = IM.status()["usable_semantics"]
    for key in ("this_module_authoritative_field", "deprecated_aliases",
                "meaning_zh", "collides_with", "never_show_together",
                "reason_zh", "rename_plan"):
        assert key in sem, f"usable_semantics 缺键 {key}"


# ==========================================================================
# 三、不得顺手改动 D-08 的 m 记账（边界守卫）
# ==========================================================================

def test_group_checked_still_present():
    """`_group_checked` 必须仍然存在 —— 本轮**不许**改 m 逻辑。

    D-08 是正确性问题，修完 `usable` 仍为 0（所需效应量 +6.3% → +5.9%，仅 0.4
    个百分点），混进本轮会污染验收。这里只做存在性守卫，
    不断言其内部数值（那属于 D-08 的验收范围）。
    """
    assert hasattr(IM, "_group_checked"), "_group_checked 被重命名/删除了"
    import inspect
    params = set(inspect.signature(IM._group_checked).parameters)
    assert params, "_group_checked 不应为无参函数"
