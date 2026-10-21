"""源码结构断言的共享工具（D-18a）。

## 为什么需要这个模块

一批测试用 `inspect.getsource(fn)` 拿到源码文本后做**子串匹配**：

    assert "cover_bonus" not in src
    assert 'llm_can_pick' in src

这有两个方向都会错的毛病：

* **假绿**：`getsource` **包含 docstring**。删掉真实调用、只在 docstring 里留着该词，
  断言仍然通过 —— 而代码已经废了。
* **假红**：在 docstring/注释里写「本函数不使用 X」是**非常自然**的说明写法，
  断言会无辜变红，**逼人去删注释而不是改代码**。

本模块提供两种正确的取法。

## 两种取法，用途不同

### `code_text(fn)` —— 剥掉 docstring 与注释，返回纯代码文本

适用：断言目标是**标识符或表达式文本**（如 `cover_bonus`、`max(1, n)`）。

    assert "cover_bonus" not in code_text(E._ticket_objective)

⚠️ **注意 `ast.unparse` 会重写字面量的引号风格**（`"llm"` → `'llm'`），
所以**含引号的断言不能用这条路**，见下。

### AST 结构查询 —— `has_call` / `has_compare`

适用：断言目标**含引号**或是**调用结构**（如 `channels.get("llm_can_pick")`、`!= "llm"`）。

    assert has_call(fn, attr="get", string_arg="llm_can_pick")
    assert has_compare(fn, op=ast.NotEq, string_const="llm")

## 实测依据（2026-10-21）

| 断言目标 | `code_text` 后仍命中？ |
|---|---|
| `prereg_scheduler` / `register_scan_candidates` / `llm_can_pick` | ✅ 是 |
| `portfolio_expected_value` / `expected_reward` / `cover_bonus` / `max(1, n)` | ✅ 是（`max(1, n)` 的空格被 unparse 保留） |
| `!= "llm"` / `channels.get("llm_can_pick")` | ❌ **否**（引号被规范化成单引号） |

所以 9 条断言里 **7 条走 `code_text`，2 条走 AST 结构查询**。
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any, Callable, Optional


def _function_node(fn: Callable) -> ast.AST:
    """把被测函数解析成 AST 节点。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError(f"{getattr(fn, '__name__', fn)} 的源码里找不到函数定义")


def _strip_docstring(fn_node: ast.AST) -> None:
    """原地剥掉函数 docstring（第一个语句若是字符串常量）。

    ## 为什么必须剥

    `getsource` 含 docstring。判据要回答的是「**代码**有没有做这件事」，
    docstring 里的说明不是代码。不剥就会两个方向都错（假绿 + 假红）。
    """
    body = getattr(fn_node, "body", [])
    if body:
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(getattr(first, "value", None), ast.Constant)
                and isinstance(first.value.value, str)):
            fn_node.body = body[1:]


def code_text(fn: Callable) -> str:
    """返回**剥掉 docstring 与注释**后的纯代码文本（`ast.unparse`）。

    适用：断言目标是标识符 / 表达式文本。
    **不适用于含引号的断言** —— unparse 会把 `"x"` 规范化成 `'x'`。
    """
    node = _function_node(fn)
    _strip_docstring(node)
    module = ast.Module(body=[node], type_ignores=[])
    return ast.unparse(module)


def _walk_body(fn: Callable):
    """遍历函数体（已剥 docstring）内的所有节点。"""
    node = _function_node(fn)
    _strip_docstring(node)
    yield from ast.walk(node)


def has_call(fn: Callable, *, attr: Optional[str] = None,
             string_arg: Optional[str] = None,
             name: Optional[str] = None) -> bool:
    """函数体里是否存在满足条件的**真实调用**。

    参数按需组合：
    * `attr`：被调用者是 `<某对象>.attr(...)`，如 `get`
    * `name`：被调用者是裸名字 `name(...)`
    * `string_arg`：调用实参里含该字符串常量

    例：`has_call(fn, attr="get", string_arg="llm_can_pick")`
    对应源码 `channels.get("llm_can_pick")`。
    """
    for node in _walk_body(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if attr is not None:
            if not (isinstance(func, ast.Attribute) and func.attr == attr):
                continue
        if name is not None:
            if not (isinstance(func, ast.Name) and func.id == name):
                continue
        if string_arg is not None:
            consts = [a for a in node.args
                      if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not any(a.value == string_arg for a in consts):
                continue
        return True
    return False


def has_compare(fn: Callable, *, op: type, string_const: Optional[str] = None,
                left_name: Optional[str] = None) -> bool:
    """函数体里是否存在满足条件的**比较表达式**。

    例：`has_compare(fn, op=ast.NotEq, string_const="llm")`
    对应源码 `<expr> != "llm"`。
    """
    for node in _walk_body(fn):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(o, op) for o in node.ops):
            continue
        if left_name is not None:
            if not (isinstance(node.left, ast.Name) and node.left.id == left_name):
                continue
        if string_const is not None:
            consts = [c for c in node.comparators
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if not any(c.value == string_const for c in consts):
                continue
        return True
    return False


def js_code(app_js_text: str) -> str:
    """剥掉 JS 注释，只留有效代码。

    ## 为什么需要（2026-10-21 实测教训）

    一批测试用「字符串是否出现」验证前端行为。若该字符串**同时出现在注释里**，
    删掉实现后断言**仍然通过** —— 典型假绿。

    实测（`test_mu2_frontend_llm.py` / `test_v10_channel_wiring.py` /
    `test_v10_frontend_cards.py` / `test_w08_frontend_auth.py` 四份文件分别踩到）：
    「照常出号」「不受系统配额限制」「WWW-Authenticate」都曾只存在于注释，
    导致对应断言假绿。

    本函数是从四份文件各自手写的 `_js_code()` **合并而来** ——
    四份正则完全一致，只 docstring 不同。合并消除「第二真源」：
    任何一份被单独修改，另外三份不会跟着变、且没人发现。

    ## 剥注释规则

    * `/* */` 块注释 —— 直接删
    * `//` 行注释 —— 删，但避开 `http://` 这类 URL 里的 `//`
    * 保留引号内的内容（用后视 `(?<![:"'])` 防止把 `"...//..."` 当注释）
    """
    import re as _re

    code = _re.sub(r"/\*.*?\*/", "", app_js_text, flags=_re.S)
    return _re.sub(r"(?<![:\"'])\s*//[^\n]*", "", code)
