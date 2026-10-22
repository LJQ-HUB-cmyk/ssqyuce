"""MU-2 前端：设置页按用户隔离的契约测试。

## 为什么这几条必须有

MU-2 把后端语义从「全局配置 + 管理员专用」改成「每账号一行」之后，
前端一度仍显示 W08 的文案（"个人配置尚未开放"），并且整卡禁用。
**界面上说不能配、后端其实能配** —— 这种错位用户只能靠猜，
而且与需求（"每个用户自己配置 LLM"）直接矛盾。

本文件把新语义钉死：来源三态可解释、Key 永不回填、清除与"留空"是两件事。
"""
from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
APP_JS = WEB_DIR / "static" / "app.js"
STYLE_CSS = WEB_DIR / "static" / "style.css"

def _js_code() -> str:
    """剥离注释后的 app.js（D-18b 合并：实现统一在 `_source_assert.js_code`）。
    ⚠️ 四份测试文件原各写一份，正则相同。合并消除「第二真源」——
       任一份被改另外三份不会跟着变且无人发现。
    """
    from _source_assert import js_code as _js_code_impl
    return _js_code_impl(APP_JS.read_text(encoding="utf-8"))


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _css() -> str:
    return STYLE_CSS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. DOM 齐备
# ---------------------------------------------------------------------------

def test_settings_dom_ids_exist():
    ids = set(re.findall(r'id="([^"]+)"', _html()))
    required = [
        "cfgUseCustomKey",      # "使用我自己的 API Key" 开关（MU-2 新增）
        "llmScopeNote",         # 来源说明
        "llmUsage", "llmUsageFill", "llmUsageNum",   # 用量条
        "btnClearKey",          # 显式清除 Key
        "cfgBaseUrl", "cfgApiKey", "cfgModel", "cfgSamples", "cfgLlmEnabled",
    ]
    missing = [i for i in required if i not in ids]
    assert not missing, f"HTML 缺少 id：{missing}"


def test_usage_bar_has_styles():
    css = _css()
    for sel in (".llm-usage", ".llm-usage-fill", ".input-with-btn"):
        assert sel in css, f"缺少样式 {sel}"


# ---------------------------------------------------------------------------
# 2. 文案不得再出现"尚未开放"
# ---------------------------------------------------------------------------

def test_no_stale_not_isolated_wording():
    """旧文案（"个人配置尚未开放"/"未隔离"）会与后端能力矛盾，必须清除。"""
    js, html = _js(), _html()
    for bad in ("尚未开放个人模型配置", "个人配置尚未开放", "个人 LLM 配置隔离尚未上线"):
        assert bad not in js, f"app.js 仍含旧文案：{bad}"
        assert bad not in html, f"index.html 仍含旧文案：{bad}"


def test_scope_three_states_are_explained():
    """三种来源都必须有**可读**的解释，尤其 none 要说明"照常出号、不是故障"。"""
    js = _js_code()   # 剥离注释：这两句在注释里也出现，用 _js() 属假绿
    # renderLlmScope 的三个分支
    assert "user:" in js and "system:" in js and "none:" in js
    assert "照常出号" in js, "none 状态必须说明仍会正常出号"
    # `or "不受配额"` 已删（D-18b 实测）：该短语在剥离注释后的 js 里 **0 次**，
    # 只存在于 4108 行的 `//` 注释 —— 它是个永远为假的分支，不掩盖任何东西，
    # 但会让读者以为「有个备用文案」。实测：删掉 3640 行的真实短语后断言会红，
    # 说明左半有牙；右半是纯死代码。
    assert "不受系统配额限制" in js, "user 态必须说明不受系统配额限制"


# ---------------------------------------------------------------------------
# 3. Key 处理的三条纪律
# ---------------------------------------------------------------------------

def test_api_key_input_never_prefilled():
    """Key 输入框**不得**被填入值。

    若把脱敏串填进 `value`，用户一保存就会把它当成真 Key 提交上去，
    于是库里存了一个"sk-****abcd"这样的假 Key —— 表现为"配置成功但一直 401"。
    只能用 placeholder 展示脱敏值。
    """
    js = _js()
    seg = js[js.index("async function loadLlmConfig()"):]
    seg = seg[:seg.index("function renderLlmScope")]
    assert '$("#cfgApiKey").value = ""' in seg, "必须显式清空 Key 输入框"
    # ⚠ D-15 抓到的真实缺陷（2026-10-22）：原写法是
    #     assert "$("#cfgApiKey").placeholder" in seg, "..."
    #   双引号冲突 —— Python 把它解析成 `assert "$("`（`#` 后全被当注释），
    #   **实际断言的是 `'$('` 这个非空字符串恒为真**，等于没测。
    #   而且不报语法错误，因为它"看起来"是合法表达式。
    # 脱敏值必须写到 `.placeholder`（左值），而不是 `.value`
    assert '$("#cfgApiKey").placeholder =' in seg, "脱敏值应走到 .placeholder 赋值"
    assert '$("#cfgApiKey").value = cfg' not in seg, "禁止把配置值回填进 Key 输入框"


def test_placeholder_marks_saved_key():
    js = _js()
    assert "已保存" in js and "留空=不修改" in js


def test_clear_key_is_explicit_separate_action():
    """清除 Key 必须是显式动作（带确认），不能靠"提交空串"隐式触发。"""
    js = _js()
    assert "function clearLlmKey()" in js
    assert "clear_api_key" in js, "清除必须走后端显式参数 clear_api_key"
    seg = js[js.index("async function clearLlmKey()"):]
    seg = seg[:seg.index("async function saveLlmConfig()")]
    assert "confirm(" in seg, "删除 Key 前必须二次确认"


def test_save_only_sends_key_when_typed():
    js = _js()
    seg = js[js.index("async function saveLlmConfig()"):]
    seg = seg[:seg.index("async function testLlmConnection()")]
    assert "if (typedKey) payload.api_key = typedKey;" in seg, (
        "必须只有用户真的输入了新 Key 才提交，否则会抹掉已存 Key")


# ---------------------------------------------------------------------------
# 4. 自备 Key 开关决定提交哪些字段
# ---------------------------------------------------------------------------

def test_custom_key_switch_controls_payload():
    """不勾「用我自己的 Key」时不应提交 base_url/model。

    否则用户只是切换"用共享通道"，却把自己那行里存的地址/模型写成了空，
    下次再切回来还得重填。
    """
    js = _js()
    assert "use_custom_key" in js
    seg = js[js.index("async function saveLlmConfig()"):]
    seg = seg[:seg.index("async function testLlmConnection()")]
    assert "if (custom) {" in seg
    assert "payload.base_url" in seg and "payload.model" in seg


def test_fields_disabled_when_not_custom():
    js = _js()
    seg = js[js.index("function toggleLlmConfig()"):]
    seg = seg[:seg.index("async function clearLlmKey()")]
    assert "enabled && custom" in seg, "地址/Key/模型只在启用且自备时可用"


# ---------------------------------------------------------------------------
# 5. 用量条只在共享通道下显示
# ---------------------------------------------------------------------------

def test_usage_bar_hidden_for_own_key_or_unlimited():
    js = _js()
    seg = js[js.index("function renderLlmUsage("):]
    seg = seg[:seg.index("function toggleLlmConfig()")]
    assert '_llmScope === "user"' in seg, "自备 Key 不受配额限制，不该显示用量条"
    assert "!quota" in seg, "配额为 0（不限）时不该显示用量条"


# ---------------------------------------------------------------------------
# 6. 前端仍走按用户端点
# ---------------------------------------------------------------------------

def test_uses_user_scoped_endpoint():
    js = _js()
    assert 'api("/api/user/llm/config"' in js
    assert 'api("/api/config/llm"' not in js, "不得回退到管理员专用端点"
