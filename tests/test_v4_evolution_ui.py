"""V5 前端结构守卫：实战战绩榜 + 进化过程。

背景（用户口径）：准确性 = 用历史数据找规律 → 用于实战 → **通过实战结果对比**。
本页面的唯一职责是呈现实战对比事实，故这里锁定四条不能退化的呈现要求：

1. **ROI 必须双口径**：`uniform` 曾因单期 1 注全中 6 红（二等奖 ¥18 万）显示 +203.9% ROI ——
   只看 `roi` 会被单期离群值严重误导，必须同时给剔除一二等奖的 `roi_ex_top`。
2. **必须显示「未校正 N 组 → 校正后 M 组」**：这是让用户理解
   「同时比多个方法时 p<0.05 不可信」的核心方法论，也是本项目最重要的一条工程习惯。
3. **只有 validated 的规律才影响出号**，且当前态（实测 9 条全 proposed）要如实呈现。
4. **重操作不得在首屏自动触发**：全量回放约 5 分钟、跑一轮进化可能超 10 分钟，
   必须由用户显式点击，并有等待反馈。

本仓库无 JS 测试运行器，故沿用既有惯例（见 tests/test_v4_honest_ui.py）用 pytest 做结构断言。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
STYLE = (WEB / "static" / "style.css").read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    """取出某个 JS 函数的完整函数体（从 function 名到下一个顶层 function）。"""
    key = f"function {name}("
    assert key in APP_JS, f"缺少函数 {name}()"
    body = APP_JS[APP_JS.index(key):]
    nxt = body.find("\nfunction ", 10)
    return body[:nxt] if nxt > 0 else body


# ---------- 1. 入口与卡片存在 ----------

def test_battle_subtab_exists_in_eval():
    """实战战绩作为「评估报告」的子页接入，不新增顶层页签。"""
    assert 'data-sub="evolution"' in INDEX
    assert "setEvalSub('evolution')" in INDEX


def test_core_cards_present():
    for cid in ("evoStatusCard", "evoBattleCard", "evoCompareCard", "evoRunCard"):
        assert f'id="{cid}"' in INDEX, f"缺少卡片 {cid}"


def test_core_renderers_exist():
    for fn in ("renderEvolutionStatus", "renderValidatedRules", "renderBattleReport",
               "renderBattleComparison", "renderEvolutionRun", "renderBattleUnavailable"):
        assert f"function {fn}(" in APP_JS, f"缺少渲染函数 {fn}()"


# ---------- 2. ROI 双口径（最重要的一条） ----------

def test_roi_renders_both_baseline_and_robust_caliber():
    """战绩榜必须同时给出 roi 与 roi_ex_top（剔除一二等奖的稳健口径）。"""
    body = _fn_body("renderBattleReport")
    assert "roi_ex_top" in body, "必须消费剔除一二等奖的稳健 ROI"
    assert "roi" in body, "原始 ROI 也要保留展示"
    # 必须解释两个口径为何不同，而不是并排放两个数字让人猜
    assert "一二等" in body or "离群" in body, "必须说明两口径差异原因（浮动奖单期离群）"


def test_roi_formatter_handles_negative_values():
    """ROI 为负是常态，格式化必须能显示负值而非退化成 '-'。"""
    assert "function fmtRoi(" in APP_JS
    body = _func = _fn_body("fmtRoi")
    assert "'-'" in body or '"-"' in body          # 仅对 null/NaN 退化
    assert "toFixed" in body


# ---------- 3. 多重检验校正提示 ----------

def test_multiple_comparison_notice_present():
    """必须显示「未校正 N 组 → 校正后 M 组」。"""
    body = _fn_body("renderBattleComparison")
    assert "n_significant_uncorrected" in body, "必须消费未校正显著组数"
    assert "n_significant_corrected" in body, "必须消费校正后显著组数"
    assert "校正" in body
    # 关键措辞：让用户明白未校正不可信
    assert ("假阳性" in body) or ("不可信" in body) or ("预期" in body), \
        "必须点明未校正 p<0.05 在同时比较多方法时不可信"


def test_pairs_render_adjusted_p_not_raw():
    """对比表必须展示校正后 p 值（p_adjusted），不能只给未校正 p。"""
    body = _fn_body("renderBattleComparison")
    assert "p_adjusted" in body, "必须展示多重检验校正后的 p 值"


def test_sample_gate_used_when_declaring_best_method():
    """样本不足时不得给出「最优方法」结论；门槛走既有 METHOD_MIN_ISSUES。"""
    body = _fn_body("renderBattleReport") + _fn_body("renderBattleComparison")
    assert ("sampleEnough" in body) or ("METHOD_MIN_ISSUES" in body), \
        "判定优劣必须过样本门槛"
    assert "样本不足" in body, "样本不足要显式说明"


# ---------- 4. 规律库与「只有 validated 才影响出号」 ----------

def test_validated_only_rule_is_explicit():
    body = _fn_body("renderValidatedRules") + _fn_body("renderEvolutionStatus")
    assert "validated" in body
    assert "影响出号" in body or "生效" in body, \
        "必须明确说明只有 validated 的规律会影响出号"
    # 三种状态都要展示，让用户看到「找了但没通过」的诚实全貌
    for st in ("proposed", "validated", "rejected"):
        assert st in body or st in APP_JS, f"规律库应展示 {st} 状态"


def test_evolution_run_shows_resolution():
    """跑完进化要展示裁决结果（挖到几条/验证几条/validated/rejected）。"""
    body = _fn_body("renderEvolutionRun")
    assert "resolution" in body, "必须展示本轮裁决结果"
    assert ("verified" in body) or ("验证" in body), "必须展示本轮验证了几条假说"


# ---------- 5. 重操作必须显式触发 + 有等待反馈 ----------

def test_heavy_operations_are_click_triggered_not_auto():
    """全量回放与跑进化只能由按钮触发，且首屏不得自动拉重接口。"""
    assert 'id="btnEvoBattle"' in INDEX
    assert 'id="btnEvoBattleFull"' in INDEX
    assert 'id="btnEvoRun"' in INDEX
    assert "runBattleReport(" in APP_JS and "runEvolutionOnce(" in APP_JS
    # 按钮文案要给出耗时预期
    assert "约 5 分钟" in INDEX, "全量回放必须提示约 5 分钟"
    assert "分钟" in INDEX


def test_long_operation_has_confirmation_and_progress():
    """长耗时操作要有二次确认与进度反馈。"""
    src = APP_JS
    assert "confirm(" in src, "重操作应有二次确认"
    for fid in ("evoBattleProgress", "evoRunProgress"):
        assert f'id="{fid}"' in INDEX, f"缺少进度容器 {fid}"


def test_battle_request_uses_small_default_window():
    """默认窗口必须是小窗口，避免用户一点就等 5 分钟。"""
    assert "runBattleReport(900)" in INDEX, "默认按钮应是近 900 期小窗口"


def test_battle_busy_guard_exists():
    """回放期间必须防重入，避免并发压垮后端。"""
    assert "_battleBusy" in APP_JS


# ---------- 6. 优雅降级 ----------

def test_module_unavailable_degrades_gracefully():
    """503 / available:false 时要提示而非报错。"""
    body = _fn_body("renderBattleUnavailable") + _fn_body("renderEvolutionStatus")
    assert "不可用" in body or "未就绪" in body or "无法读取" in body


# ---------- 7. 缓存击穿（DEPLOY.md 强制约定） ----------

def test_cache_bust_version_bumped_to_20261015_2():
    """V5 前端有实质改动，?v= 必须从 20261015.1 升到 20261015.2。

    这条断言刻意写成「严格大于上一版」而非硬编码未来版本号：
    下次前端改动只需再升版本号，不必来改这个测试；但它仍然能抓住真实回归 ——
    把版本号改回 20261015.1 或更早，测试必须失败。
    """
    m_app = re.search(r"app\.js\?v=(\d{8})\.(\d+)", INDEX)
    m_css = re.search(r"style\.css\?v=(\d{8})\.(\d+)", INDEX)
    assert m_app, "index.html 缺少 app.js 的 ?v= 缓存击穿参数"
    assert m_css, "index.html 缺少 style.css 的 ?v= 缓存击穿参数"

    def _as_tuple(m):
        return (int(m.group(1)), int(m.group(2)))

    assert _as_tuple(m_app) > (20261015, 1), (
        f"app.js ?v= 必须比上一版 20261015.1 新，实际 {m_app.group(0)}")
    assert _as_tuple(m_css) > (20261015, 1), (
        f"style.css ?v= 必须比上一版 20261015.1 新，实际 {m_css.group(0)}")
    assert _as_tuple(m_app) == _as_tuple(m_css), (
        "app.js 与 style.css 的版本号应保持一致，避免只击穿一半缓存")


def test_style_has_rule_for_new_cards():
    """新卡片需要样式（至少验证/拒绝态有可辨识的配色类）。"""
    assert ".evo-" in STYLE or "evoStatusCard" in STYLE or "battle" in STYLE, \
        "新卡片缺少样式定义"
