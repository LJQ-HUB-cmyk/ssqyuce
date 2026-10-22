"""D-21：`error` 字段不得泄漏内部实现名。

## 为什么要这条守卫

用户看到 `battle_report()` 会以为**是自己操作错了**（带括号像命令），
去改参数、去搜 `lottery.evolution` —— **消耗用户时间而不给任何可行动作**。

## 判据：区分 `error` 与 `detail`

内部名不是不能留，是**不能留在用户直接读的 `error` 里**。
`detail` 是给维护者排查用的，可以放。
"""
from __future__ import annotations

import ast
import pathlib
import re


# 内部实现名的形态（在 `error` 字符串里出现即违规）
_INTERNAL_PATTERNS = [
    (r"\blottery\.\w+", "Python 模块路径（如 lottery.battle）"),
    (r"\b[a-zA-Z_]\w*\(\)", "函数调用形态（如 battle_report()）—— 像命令，诱导用户操作"),
]


def _error_literals() -> list[tuple[str, int, str]]:
    """收集 routes_*.py 里所有 `"error": "..."` 的字符串字面量。"""
    out = []
    for py in sorted(pathlib.Path("lottery").glob("routes_*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "error"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    out.append((py.name, node.lineno, v.value))
    return out


def test_error_field_does_not_leak_internal_implementation_names():
    """`error` 是用户直接读的字段，**不得出现模块路径或函数调用形态**。

    ## 为什么这条不能靠「注意一下」而必须是测试

    D-21 盘点时 22 条错误文案里有 6 条泄漏内部名（A 档）。
    这类问题**每次新增错误分支都可能复发**，而它不影响任何功能 ——
    没有测试就会一直累积。

    ## 变异实证

    把任一 `"error": "...",` 改回 `"error": "battle_report() 执行失败"` → 本测试必红。
    """
    offenders = []
    for fname, lineno, text in _error_literals():
        for pat, why in _INTERNAL_PATTERNS:
            if re.search(pat, text):
                offenders.append(f"  {fname}:{lineno}  {why}\n    文案: {text[:70]}")
    assert not offenders, (
        "`error` 字段出现内部实现名 —— 用户看不懂也做不了任何正确动作：\n"
        + "\n".join(offenders)
        + "\n\n正确做法：用户文案写「暂时无法生成，这不是你操作的问题」，"
          "把内部名移到 `detail` 字段（给维护者排查用）。"
    )


# D-20 待办：「信息量过低」的 error 文案清单（D-21 不修，因为它不是「泄漏内部名」）。
#
# 为什么分开：D-21 的判据是「泄漏了用户不该看到的东西」（说太多），
# D-20 的判据是「没告诉用户下一步能做什么」（说太少）。**两者修法不同**：
#   D-21 → 把内部名移到 detail
#   D-20 → 补上「这不是你的问题 / 稍后重试 / 若持续请反馈」
# 混在一起修会让「D-21 完成」看起来像「文案都好了」，那是假的。
_D20_KNOWN_LOW_INFO = {
    "routes_rules.py": ["标注失败", "扫描失败", "注册失败", "回放失败", "刷新失败", "阶段分析失败"],
    "routes_tasks.py": ["暂无数据"],
}


def test_error_field_is_not_empty_or_placeholder():
    """`error` 不许退化成**空串或无文本**（真正无法定位的形态）。

    ⚠ 本测试**不**断言「信息量是否足够」—— 那是 D-20 的范围（见上方清单）。
    D-21 只负责「不许泄漏内部名」，两类问题修法不同，混在一起会让
    「D-21 完成」被误读成「文案都改好了」。
    """
    bad = []
    for fname, lineno, text in _error_literals():
        t = text.strip()
        if not t or t.lower() == "error":
            bad.append(f"  {fname}:{lineno}  {t!r}")
    assert not bad, (
        "`error` 字段为空或仅为 'error'，完全无法定位：\n" + "\n".join(bad))


def test_d20_low_info_errors_are_tracked_not_silently_ignored():
    """把「信息量过低」的文案**逐条登记**，防止默默留着。

    `_D20_KNOWN_LOW_INFO` 里每一条都是「用户看不出下一步做什么」的实例。
    本测试不要求现在修，但要求：**新增低信息文案必须同步登记**，
    否则 D-20 的待办清单会失真。
    """
    known = {(f, t) for f, ts in _D20_KNOWN_LOW_INFO.items() for t in ts}
    unknown = []
    for fname, lineno, text in _error_literals():
        t = text.strip()
        if len(t) < 8 and (fname, t) not in known:
            unknown.append(f"  {fname}:{lineno}  {t!r}")
    assert not unknown, (
        "出现未登记的低信息量 error 文案（要么补全，要么登记进 _D20_KNOWN_LOW_INFO）：\n"
        + "\n".join(unknown))
