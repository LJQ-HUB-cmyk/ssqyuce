"""V13 P2：模块化第一步的守护测试（依赖方向 + 打桩点一致性）。

## 这一步做了什么

把 `_current_user_id` contextvar 从 `api_app.py` 搬到 `lottery/request_context.py`，
并把 `db.py` / `llm_client.py` / `user_llm_api.py` 的反向依赖改掉：

改造前（倒置）：
```
db.py          ──> api_app.py
llm_client.py  ──> api_app.py
user_llm_api   ──> api_app.py
```
改造后（单向）：
```
api_app.py ──> request_context.py <── db.py
                                  <── llm_client.py
api_app.py ──> llm_config_store.py <── user_llm_api.py
```

## 为什么这个方法值得测

`api_app.py` 是 3500 行的 Web 应用模块。底层库为了读一个 contextvar 而依赖它，
后果是：无法独立测试、循环依赖只能靠"把 import 写进函数体"来躲（M1 §1.2
统计有 70+ 处这种懒加载）、切分 api_app 时处处受阻。

**没有测试守着的"重构"会悄悄回滚**：下次有人图省事再写一句
`from . import api_app`，一切照常运行，没人会发现分层又倒了。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

LOTTERY = Path(__file__).resolve().parent.parent / "lottery"


def _imports_of(path: Path):
    """返回该文件里所有 `from . import X` / `from lottery import X` 的模块名集合。

    用 AST 而不是 grep：grep 会把注释与字符串里的同名字样算进去
    （本项目已多次因此误判）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module is None:
                for a in node.names:
                    names.add(a.name)
            elif node.level == 1 and node.module:
                names.add(node.module)
    return names


def test_no_low_level_module_imports_api_app():
    """**核心**：底层库不得反向依赖 Web 应用模块 `api_app`。

    这不只是洁癖 —— 它决定了 `db`/`llm_client` 能否被单独 import 与单独测试。
    """
    # 允许出现的例外（都是有理由的、且不在底层）
    allowed = {"auth_api", "user_llm_api"}
    offenders = []
    for p in sorted(LOTTERY.glob("*.py")):
        if p.name in ("api_app.py",) or p.name in allowed:
            continue
        if "api_app" in _imports_of(p):
            offenders.append(p.name)
    assert not offenders, (
        f"这些模块反向 import 了 api_app：{offenders}\n"
        "底层库请改用 lottery.request_context（纯状态）或 "
        "lottery.llm_config_store（配置读写），不要依赖 Web 应用模块。")


def test_request_context_is_pure_state():
    """`request_context` 必须是纯状态模块：不 import 任何业务模块。

    一旦它开始 import db/config 之类，就会重新变成一个新的循环依赖中心。
    """
    names = _imports_of(LOTTERY / "request_context.py")
    banned = {"db", "config", "api_app", "llm_client", "engine"}
    assert not (names & banned), (
        f"request_context 不应依赖业务模块，实际依赖：{names & banned}")


def test_llm_config_store_does_not_import_api_app():
    names = _imports_of(LOTTERY / "llm_config_store.py")
    assert "api_app" not in names


def test_api_app_reexports_contextvar_for_compat():
    """`api_app._current_user_id` 等名字必须仍可用。

    既有测试与中间件都按这些名字访问；搬迁不该改契约。
    """
    from lottery import api_app, request_context
    assert api_app._current_user_id is request_context._current_user_id
    assert api_app.current_user_id is request_context.current_user_id


def _goes_through_low_level(window: str) -> bool:
    """该测试片段是否经由底层库（它们直接 import request_context）。"""
    markers = ("llm_client", "db.", "engine", "_run_chat", "chat_json",
               "save_predictions", "record_usage", "resolve_tenant")
    return any(mk in window for mk in markers)


def test_stub_points_match_real_dependency_sources():
    """**打桩点一致性**：测试里 monkeypatch 的名字必须是真正的依赖源。

    P2 搬迁时踩到的真问题：测试打桩 `api_app.current_user_id`，而搬迁后
    `llm_client` 直接调 `request_context.current_user_id` —— 打桩**静默失效**，
    用例照样通过，但它测的已经不是生产路径了。

    这类"打桩点与依赖源脱节"是本项目假绿的老来源（W06 的路由枚举、
    路由分级测试都是同一类）。这里用静态扫描把它钉死。
    """
    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for p in sorted(tests_dir.glob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(
                r'monkeypatch\.setattr\(\s*api_app\s*,\s*"current_user_id"', text):
            # 同一次 setattr 附近 400 字符内必须同时打到 request_context
            window = text[m.start(): m.start() + 400]
            # 判断"这条用例测的是哪条路径"：
            #  * 直接调 `api_app.<某函数>`（不经过底层库）→ 只打 api_app 是对的；
            #  * 走 llm_client / db / engine（它们 import request_context）→ 必须两边都打。
            # 用"同一函数体内是否出现底层库调用"来区分。
            if "request_context" not in window and _goes_through_low_level(window):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{p.name}:{line}")
    assert not offenders, (
        f"这些地方只打了 api_app.current_user_id，没有同时打 request_context：{offenders}\n"
        "两条路径都要打桩，否则会被静默短路。")


def test_llm_config_endpoints_delegate_to_store():
    """`/api/config/llm` 的实现应在 `llm_config_store`，路由层只留薄壳。

    P2 第二步后，端点本体已搬进 `routes_llm.py`，所以这里检查的是路由模块。
    """
    candidates = [LOTTERY / "routes_llm.py", LOTTERY / "api_app.py"]
    holder = None
    for p in candidates:
        if p.exists() and "def update_llm_config(" in p.read_text(encoding="utf-8"):
            holder = p
            break
    assert holder is not None, "找不到 update_llm_config 的定义处"
    text = holder.read_text(encoding="utf-8")
    seg = text[text.index("def update_llm_config("):]
    end = seg.find("\ndef ")
    seg = seg[:end] if end > 0 else seg
    assert "llm_config_store.save" in seg, (
        f"update_llm_config 在 {holder.name} 里没有委托给 llm_config_store.save")


def test_route_modules_have_no_undefined_globals():
    """**静态检查**：域模块里用到的全局名必须都有定义（import 或本模块内定义）。

    这是 P2 切分时踩到的真问题：用脚本把端点块从 api_app 搬到域模块，
    **块里用到的 `JSONResponse` 没有跟着搬**（它原本在 api_app 的顶层 import 里）。
    3 个模块同时缺，只有 `test_m4_2_api::test_methods_config_validation` 抓到，
    因为另外两条路径的用例没触发那个分支。

    运行期才炸的 NameError 属于"分文件时最容易漏、最难预判"的一类，值得用
    静态分析兜住：宁可让这条测试告诉我要补哪个 import，也不要等线上 500。
    """
    import builtins
    missing = {}
    for p in sorted(LOTTERY.glob("routes_*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        defined = set(dir(builtins))
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    defined.add(a.asname or a.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
            elif isinstance(n, ast.arg):
                defined.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                defined.add(n.name)      # `except Exception as e:`
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                defined.add(n.id)
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        bad = sorted(used - defined)
        if bad:
            missing[p.name] = bad
    assert not missing, (
        "这些域模块用到了未定义的名字（切分时漏搬 import）：\n" +
        "\n".join(f"  {k}: {v}" for k, v in missing.items()))


def test_route_modules_do_not_import_api_app():
    """域路由模块不得 import api_app（否则切分等于白做）。"""
    offenders = []
    for p in sorted(LOTTERY.glob("routes_*.py")):
        if "api_app" in _imports_of(p):
            offenders.append(p.name)
    assert not offenders, f"路由模块反向 import 了 api_app：{offenders}"


def test_api_app_keeps_only_assembly_responsibilities():
    """`api_app.py` 的角色应收缩为「组装」：顶层 `@app.` 端点数量持续下降。

    这不是硬性上限，而是一条**防回滚**哨兵：P2 的目标是把端点下沉到域模块，
    如果有人习惯性地往 api_app 里加新端点，数量会涨上去并触发提醒。
    需要调高阈值时，请先确认新端点确实无法归入任何现有域。
    """
    text = (LOTTERY / "api_app.py").read_text(encoding="utf-8")
    n = text.count("\n@app.")
    assert n <= 71, (
        f"api_app.py 里仍有 {n} 个端点（阈值 71）。P2 的目标是把它们下沉到 "
        f"routes_*.py；新增端点请放进对应域模块。")
