"""D-15：`assert ... or True` 这类**恒真断言**不得回到测试里。

## 为什么这是最坏的一种测试

`assert X or True` 恒为真 —— **它不会红，也不会绿得有理由**。
它只做一件事：让读者以为「这里有检查」。

本项目曾有 5 处（D-15 清单），全部已修。本测试防止它们回来。

## 判据范围

只查 `assert` 语句里**真的带了 `or True`** 的形态（AST 级），
不看注释 —— 历史修复记录里会提到 `or True`（那是证据，要保留）。
"""
from __future__ import annotations

import ast
import pathlib
import re


def _is_or_true(node: ast.AST) -> bool:
    """判断表达式是否形如 `X or True`（任意嵌套层级）。

    用 AST 结构判定，不做文本匹配 —— 文本会跨行误伤相邻代码。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.BoolOp) and isinstance(sub.op, ast.Or):
            for v in sub.values:
                if isinstance(v, ast.Constant) and v.value is True:
                    return True
    return False


def _test_files():
    return sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py"))


def test_no_assert_or_true_in_test_suite():
    """全测试套件不得存在 `assert ... or True`。

    ## 变异实证

    往任一测试文件加 `assert False or True` → 本测试必红。
    """
    bad = []
    for f in _test_files():
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            # ⚠ 必须走 AST 结构，不能用 `get_source_segment` + 正则。
            # 首版用文本正则时误报了 `test_mu2_frontend_llm.py:106` ——
            # 那条断言本身正常，但因为 segment 跨到相邻行，正则匹配到了别处的 `or True`。
            # **这又是「判据的输入与要验证的事实不是同一个东西」。**
            if _is_or_true(node.test):
                bad.append(f"  {f.name}:{node.lineno}  {ast.unparse(node.test)[:70]}")
    assert not bad, (
        "检测到恒真断言（`or True` 让断言永真，等于没测）：\n"
        + "\n".join(bad)
        + "\n\n正确做法：删掉 `or True`，或改成真正的断言；"
          "若该断言确实无法验证，就删掉它并在注释里写明原因。")


def test_no_assert_whose_condition_is_a_bare_string():
    """`assert "某个字符串"` —— 恒真，且往往是**引号冲突后的残骸**。

    ## 真实案例（2026-10-22 抓到）

    ```python
    assert "$("#cfgApiKey").placeholder" in seg   # ← 想断言子串包含
    ```
    双引号冲突，Python 解析成 `assert "$("`（`#` 后全是注释）。
    **不报语法错误**，`pytest` 显示 PASSED，覆盖率也计入。

    修法不是「删掉」，而是**改成正确的断言目标** —— 本例原本连断言的左值都选错了。
    """
    bad = []
    for f in _test_files():
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
                if isinstance(node.test.value, str):
                    bad.append(f"  {f.name}:{node.lineno}  assert {node.test.value!r}")
    assert not bad, (
        "以下 assert 的条件是裸字符串（恒真，通常是引号冲突残留）：\n"
        + "\n".join(bad)
        + "\n\n检查是不是把 `assert \"X\" in src` 写成了引号冲突形态。")


def test_no_assert_with_constant_true_condition():
    """更宽的一种：`assert True` / `assert 1` 这类**字面量恒真**断言。"""
    bad = []
    for f in _test_files():
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            t = node.test
            # 只看 `Assert.test` 本身是不是常量 —— **不能 ast.walk**：
            # `assert "x" in seg` 的 Compare 里也有 Constant 操作数，
            # walk 会把它们当违规（首版就这样误报了 test_mu2_frontend_llm.py:106）。
            if isinstance(t, ast.Constant) and t.value:
                bad.append(f"  {f.name}:{node.lineno}  assert {t.value!r}")
    assert not bad, (
        "检测到字面量恒真断言：\n" + "\n".join(bad))
