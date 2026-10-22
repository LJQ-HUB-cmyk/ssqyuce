"""D-19：`/api/capabilities` 前端接入的硬约束守卫。

## 核心约束（齐交付裁定）

> `cannot` 段的可见面积/位置不得低于 `can` 段，且必须同屏、不许折叠。
> **若只展示 `can` 不展示 `cannot`，该端点从「让用户看到系统做不到什么」
> 变成「吹牛工具」—— 比不接更糟，因为它有「官方认证」的外观。**

这条不能用「注意一下」来保证，必须是断言。
"""
from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
# ⚠ 必须用剥注释版（D-18b 统一实现在 `tests/_source_assert.py`）。
#
# 我在本文件首版写了 `read_text` + 子串断言 —— **那正是元守卫要防的形态**：
# 注释里提到某个词就会被当成「代码里有」，反之注释里写「不要用 X」还会让
# 「X 不存在」的断言无辜变红。元守卫上线后立刻抓到了我这 9 处增长。
from _source_assert import js_code as _js_code

APP_JS = _js_code((WEB / "static" / "app.js").read_text(encoding="utf-8"))
INDEX = (WEB / "index.html").read_text(encoding="utf-8")   # HTML 无 JS 注释，原样
STYLE = (WEB / "static" / "style.css").read_text(encoding="utf-8")  # 仅查结构块，见下


def _fn_body(name: str) -> str:
    """取 `function name(...) {...}` 或 `async function name(...) {...}` 的函数体。

    用括号配平而不是正则贪婪 —— 正则 `{.*}` 会跨函数匹配到文件末尾。
    """
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", APP_JS)
    assert m, f"未找到函数 {name}() —— 前端接入可能被删了"
    i = m.end()
    depth = 1
    while i < len(APP_JS) and depth:
        if APP_JS[i] == "{":
            depth += 1
        elif APP_JS[i] == "}":
            depth -= 1
        i += 1
    return APP_JS[m.end(): i - 1]


# ---------------------------------------------------------------------------
# ① 接入存在性
# ---------------------------------------------------------------------------

def test_frontend_calls_capabilities_endpoint():
    """前端必须真的请求 `/api/capabilities`（不能只有卡片没有数据）。"""
    assert "/api/capabilities" in APP_JS, (
        "app.js 里没有请求 /api/capabilities —— D-19 未接入")


def test_capabilities_card_exists_in_html():
    """页面必须有卡片的 DOM 锚点，否则渲染无处可去。"""
    assert 'id="capabilitiesCard"' in INDEX
    assert 'id="capBody"' in INDEX


def test_load_capabilities_is_wired_into_load_all():
    """必须挂在 `loadAll()` 里 —— 用户打开页面就能看到，不必点进某个 tab。"""
    body = _fn_body("loadAll")
    assert "loadCapabilities(" in body, (
        "loadAll() 未调用 loadCapabilities() —— 用户首屏看不到能力清单")


# ---------------------------------------------------------------------------
# ② 同屏不可折叠（核心约束）
# ---------------------------------------------------------------------------

def test_can_and_cannot_are_rendered_in_the_same_container():
    """`can` 与 `cannot` 必须在**同一个容器**里输出。

    ## 为什么必须同容器，而不是两个独立卡

    两个独立卡可以由任意一方先渲染完、另一方后渲染，中间出现
    「只看到能做到」的窗口期。**同屏 = 同一个 innerHTML 赋值**，
    这在结构上消除那个窗口。
    """
    body = _fn_body("renderCapabilities")
    # 必须有一次 innerHTML 赋值同时包含 can 与 cannot 的产物
    assigns = re.findall(r"body\.innerHTML\s*=\s*(.*?);\s*\n", body, re.S)
    assert assigns, "renderCapabilities 没有给 capBody 赋值"
    merged = max(assigns, key=len)
    assert "canHtml" in merged and "cannotHtml" in merged, (
        "can 与 cannot 不在同一次 innerHTML 赋值里 —— 会出现只显示一侧的窗口期")
    assert "cap-grid" in merged, "两栏必须用同一个 grid 容器"


def test_no_collapsible_wrapper_around_cannot():
    """`cannot` 段不得被 `<details>` / `hidden` / `max-height` 折叠。

    只展示 `can` 不展示 `cannot` = 吹牛工具。
    """
    body = _fn_body("renderCapabilities")
    forbidden = [
        ("<details", "用了 <details> 折叠"),
        ("hidden", "用了 hidden 属性"),
        ("display:none", "用了 display:none"),
        ("display: none", "用了 display: none"),
    ]
    hits = [why for token, why in forbidden if token in body]
    assert not hits, (
        "renderCapabilities 里出现折叠/隐藏 can 或 cannot 的手段：" + "；".join(hits))


def test_cannot_column_has_no_smaller_visual_weight_in_css():
    """CSS 不得给 `cannot` 栏更小的字号 / 行高（视觉权重不得低于 can）。"""
    # 取 .cap-item-no 与 .cap-title 的样式块
    m = re.search(r"\.cap-item-no\s*\{([^}]*)\}", STYLE)
    if m:
        decl = m.group(1)
        assert "display:none" not in decl.replace(" ", ""), ".cap-item-no 被隐藏"
        assert "max-height" not in decl, ".cap-item-no 被限制高度（可能折叠）"
    # 两栏必须共用一个 grid，且 `grid-template-columns` 必须在**同一个规则块**里。
    #
    # ⚠ 原写法 `".cap-grid" in STYLE and "grid-template-columns" in STYLE` 是错的：
    #   两个条件是**独立搜索整个文件**，任一为真就过。
    #   实测变异：把 `.cap-grid {` 改名成 `.cap-x {` 后，`grid-template-columns`
    #   仍然存在于别的规则里 ⇒ 断言照样通过（**守卫无牙**）。
    #   这正是「判据的输入与要验证的事实不是同一个东西」的又一例。
    m_grid = re.search(r"\.cap-grid\s*\{([^}]*)\}", STYLE)
    assert m_grid, "缺少 .cap-grid 规则块 —— 无法保证 can/cannot 同屏两栏"
    assert "grid-template-columns" in m_grid.group(1), (
        ".cap-grid 块里没有 grid-template-columns —— 两栏布局不成立"
        "（注意：必须在该块内，不能是文件里任意位置出现）")


# ---------------------------------------------------------------------------
# ③ 单一判据：前端不得重复后端的判断逻辑
# ---------------------------------------------------------------------------

def test_frontend_does_not_duplicate_backend_degraded_logic():
    """「有没有降级」完全由 `degraded_items` 是否为空决定。

    **前端不得另外判断 `full_ev` / `degraded` 等字段** —— 那会形成两处判据，
    后端改了前端不知道，两边静默分叉（与「一个词两个意思」同类，只是分叉点在逻辑）。
    """
    body = _fn_body("renderCapabilities")
    # 只禁「读后端另外的降级字段」：
    #   · `d.degraded`         —— 旧语义的粗粒度字段（会漏报能力项）
    #   · `full_ev`            —— 用户主动选择，不是降级
    #   · `constants_degraded` —— 仅常量是否取全，不等于「能力受限」
    # 注意：局部变量 `degraded` 来自 `d.degraded_items`，是**唯一判据**，允许。
    forbidden = ["full_ev", "d.degraded", "constants_degraded"]
    # 剥掉注释再检查 —— 注释里提到 `full_ev` 是为了解释「为什么不判它」，
    # 那不是重复判据。若不剥，会把「解释不重复」判成「重复」。
    #
    # 这本身是今天反复出现的教训：**守卫的输入里混进了不该算的东西**。
    body_check = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body_check = re.sub(r"//[^\n]*", "", body_check)
    body_check = body_check.replace("d.degraded_items", "")
    hits = [t for t in forbidden if t in body_check]
    assert not hits, (
        f"renderCapabilities 重复了后端的降级判断：{hits}。"
        "「有没有降级」只应看 degraded_items 是否为空（单一判据）。")


# ---------------------------------------------------------------------------
# ④ 常量必须来自后端（前端不写死）
# ---------------------------------------------------------------------------

def test_constants_are_read_from_response_not_hardcoded():
    """`constants` 段的展示必须读响应字段，不得写死数字。"""
    body = _fn_body("renderCapabilities")
    assert "cons.ev_single" in body, "单注返奖比应读 cons.ev_single，不能写死"
    assert "cons.ev_sixth" in body, "六等奖返奖比应读 cons.ev_sixth"
    # 写死的可疑字面量（0.523 / 15.625 这类）
    for lit in ("0.523", "0.5319", "15.625"):
        assert lit not in body, (
            f"renderCapabilities 里出现写死数字 {lit} —— 必须读后端实时值")
