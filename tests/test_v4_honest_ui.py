"""V4：前端诚实口径结构守卫。

背景（用户实报「准确性过低」）：系统**没有**预测能力（双色球独立随机，已用 3514 期
数据验证与均匀随机不可区分），但 UI 曾**暗示**它有能力。两条真实事故：
  1. 前端显示「蓝球命中率 10.0%」并对照「理论随机 6.25%」——那只是 1 期 10 注的样本；
  2.「最佳方法」曾被封给一个已废弃的模型（仅 1 期样本）。

本仓库没有 JS 测试运行器，故沿用既有惯例（见 test_v3_u9_frontend_track_record.py）
用 pytest 做前端结构断言，锁住「不再把随机波动当能力」这条真实回归。

反向验证：本文件全部断言在 V4 改动前的 app.js / index.html 上应当失败
（改动前无 metricWithSample / rigorCard / ?v=20261015.1 / V4 版本号）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")


# ---------- 1. 样本量门槛与「样本不足」提示 ----------

def test_insufficient_sample_notice_exists():
    """必须存在「样本不足，不构成结论」的统一提示文案。"""
    assert "样本不足，不构成结论" in APP_JS, "缺少样本不足提示"


def test_metric_requires_sample_size():
    """比率类指标必须经统一入口渲染，且带上样本量。"""
    assert "function metricWithSample(" in APP_JS, "缺少带样本量的指标渲染入口"
    body = APP_JS[APP_JS.index("function metricWithSample("):]
    body = body[:body.index("\nfunction ", 10)]
    # 样本不足时不得展示百分比
    assert "样本不足" in body
    assert "样本 " in body, "必须展示样本量"


def test_frontend_threshold_not_hardcoded_twice():
    """前端门槛必须来自后端常量，不得出现第二个写死的 60。"""
    assert "METHOD_MIN_ISSUES" in APP_JS, "缺少前端门槛变量"
    # 门槛变量必须由 /api/info 的 method_min_issues 赋值
    assert "method_min_issues" in APP_JS, "门槛应取自 /api/info，不得前端自行写死"
    # 阈值字面量只允许出现在向后端对齐的常量声明处
    literals = re.findall(r"=\s*60\s*;", APP_JS)
    assert literals, "应保留与后端对齐的常量声明"
    assert "TRACK_METHOD_MIN_ISSUES = 60" in APP_JS


def test_track_record_ratios_go_through_sample_gate():
    """历史战绩卡的蓝球命中率/≥五等奖率必须走样本门槛，不得裸展示百分比。"""
    body = APP_JS[APP_JS.index("function renderTrackRecord("):]
    body = body[:body.index("\nfunction ", 10)]
    assert 'metricWithSample("蓝球命中率"' in body, "蓝球命中率必须经样本门槛"
    assert 'metricWithSample("≥五等奖率"' in body, "≥五等奖率必须经样本门槛"
    # 事故原文：曾对照「理论随机 6.25%」把这个比率暗示成能力
    assert '理论随机 6.25%' not in body, "不得再把裸命中率对照理论值展示为能力"
    assert "不构成结论" in body, "样本不足时必须显式说明不构成结论"


# ---------- 2. 随机性检验卡 ----------

def test_randomness_card_exists_in_html():
    """页面必须有「随机性检验」卡（让用户理解为什么准确率上不去）。"""
    assert 'id="rigorCard"' in INDEX, "缺少随机性检验卡容器"
    m = re.search(r'id="rigorCard".*?</h2>', INDEX, re.S)
    assert m, "未找到随机性检验卡标题"
    assert "随机性检验" in m.group(0)
    assert 'id="rigorTag"' in m.group(0)


def test_randomness_card_shows_min_detectable_effect():
    """卡片必须展示「在当前 N 期样本量下能检出的最小提升」。"""
    body = APP_JS[APP_JS.index("function renderRigorCard("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "min_detectable_effect" in body, "必须消费最小可检出效应"
    assert "期样本量下" in body, "必须以样本量口径表述最小可检出提升"
    assert "不可区分" in body, "必须说明数据与随机不可区分"


def test_randomness_card_degrades_gracefully():
    """rigor 依赖缺失时必须明确降级，绝不能伪装成「检验通过」。"""
    assert "function renderRigorUnavailable(" in APP_JS
    body = APP_JS[APP_JS.index("function renderRigorUnavailable("):]
    body = body[:body.index("\nfunction ", 10)]
    assert "不代表" in body, "必须说明不可用不等于检验通过"
    # loadRigor 必须有 try/catch 兜底（503 时不炸掉首页）
    load = APP_JS[APP_JS.index("async function loadRigor("):]
    load = load[:load.index("\nasync function ", 10)] if "\nasync function " in load[10:] else load
    assert "catch" in load, "loadRigor 必须捕获异常"
    assert "available === false" in load, "必须处理 available=false 降级结构"


def test_signal_path_reports_corrected_p():
    """策略显著性必须展示多重检验校正后的 p 值，而不是裸观察均值。"""
    assert "adjusted_p" in APP_JS, "必须展示校正后 p 值"
    assert "survives_correction" in APP_JS, "必须展示是否幸存多重检验校正"
    body = APP_JS[APP_JS.index("async function renderRigorSignal("):]
    body = body[:body.index("\nasync function ", 10)]
    assert "多重检验校正" in body


# ---------- 3. 诚实边界文案 ----------

def test_honesty_boundary_text_matches_readme():
    """诚实边界文案须与 README 一致：双色球为独立随机事件，本系统不承诺中奖率。"""
    assert "双色球为独立随机事件，本系统不承诺中奖率" in APP_JS
    assert "双色球为独立随机事件，本系统不承诺中奖率" in INDEX


def test_no_hardcoded_period_placeholder_introduced():
    """不得引入新的未替换期数占位符。"""
    assert "近 N 期" not in APP_JS
    assert "近 N 期" not in INDEX


# ---------- 4. 版本号与缓存击穿 ----------

def test_cache_bust_version_bumped():
    """app.js / style.css 的 ?v= 必须升到 20261015.1（DEPLOY.md 强制约定）。"""
    assert "app.js?v=20261015.1" in INDEX, "app.js 缓存击穿参数未提升"
    assert "style.css?v=20261015.1" in INDEX, "style.css 缓存击穿参数未提升"
    assert "app.js?v=20261007.2" not in INDEX, "旧版本号仍存在"


def test_app_version_and_milestone_bumped():
    """config.py 的 APP_VERSION/APP_BUILD 与里程碑必须含 V4 条目。"""
    from lottery import config
    assert config.APP_VERSION == "2.0.0", f"APP_VERSION 应为 2.0.0，实际 {config.APP_VERSION}"
    assert config.APP_BUILD == "2026-10-V4", f"APP_BUILD 应为 2026-10-V4，实际 {config.APP_BUILD}"
    assert "V4" in config.APP_MILESTONES, "APP_MILESTONES 缺少 V4 条目"


# ---------- 5. 后端：端点存在 + 缺依赖时优雅降级 ----------

def test_rigor_endpoints_registered():
    """三个 V4 端点必须已注册，且不在导入期因缺 rigor 而失败。"""
    from lottery.api_app import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/rigor/randomness" in paths
    assert "/api/rigor/signal" in paths
    assert "/api/portfolio/expected-value" in paths


def test_info_exposes_backend_threshold():
    """/api/info 必须下发后端门槛常量，供前端复用同一数字。"""
    from lottery.api_app import app_info
    from lottery import config
    info = app_info()
    assert info.get("method_min_issues") == config.METHOD_RECOMMENDATION_MIN_ISSUES
    assert "不承诺中奖率" in info.get("honesty_boundary", "")


def test_rigor_missing_module_does_not_break_startup(monkeypatch):
    """rigor 模块缺失时必须 503 + available=false，服务不得启动失败。

    为什么不能用打桩 ``builtins.__import__`` 的方式模拟（这里前后踩了三次坑）：

    1. **``sys.modules["lottery.rigor"] = None`` 拦不住**：模块已被别的用例导入过
       （全量跑时必然如此）时，直接命中缓存返回真模块。
    2. **只清 ``sys.modules`` + 父包属性也拦不住**：``rigor.py`` 真实存在于磁盘，
       import 系统会重新从磁盘把它加载回来。
    3. **打桩 ``builtins.__import__`` 仍旧拦不住**：``from . import rigor`` 编译出来的是
       ``__import__("lottery", fromlist=("rigor",))`` —— 传给 ``builtins.__import__`` 的
       name 是 **``"lottery"``** 而非 ``"lottery.rigor"``；随后的子模块加载由
       ``importlib._handle_fromlist`` 完成，走 ``_bootstrap._call_with_frames_removed``，
       完全绕过 ``builtins.__import__``。所以打在 ``builtins.__import__`` 上的桩
       永远不会命中（实测记录到的调用全是 ``('lottery', ('rigor',), 0)``）。

    唯一可靠的做法：在 ``sys.meta_path`` 插入一个拒绝该模块的 finder，让子模块查找直接失败。
    """
    import importlib.abc
    import sys
    from fastapi.testclient import TestClient
    from lottery import api_app
    import lottery

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in ("lottery.rigor", "rigor"):
                raise ImportError("simulated: lottery.rigor not delivered")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])
    # 同时清掉既有缓存与父包属性，确保走的是「真实缺模块」路径
    monkeypatch.delattr(lottery, "rigor", raising=False)
    monkeypatch.delitem(sys.modules, "lottery.rigor", raising=False)
    monkeypatch.delitem(sys.modules, "rigor", raising=False)
    # 注：以前这里还要把 config.DB_PATH 指回真实库，因为别处曾用直接赋值
    # （config.DB_PATH = tmp_path/...）把数据目录换成临时空库，本端点会先撞上
    # 「本地暂无开奖数据」的 400 而非 503。该污染源已在 tests/test_v4_coverage.py
    # 改为 monkeypatch.setattr，故此处不再需要兜底。

    client = TestClient(api_app.app)
    r = client.get("/api/rigor/randomness")
    assert r.status_code == 503, f"缺模块应降级为 503，实际 {r.status_code}"
    d = r.json()
    assert d.get("available") is False
    assert d.get("ok") is False
    # 其它端点不受影响
    assert client.get("/api/health").status_code == 200

    # 恢复后同一端点必须能正常工作（证明降级没留下坏状态）
    monkeypatch.undo()
    monkeypatch.delattr(lottery, "rigor", raising=False)
    monkeypatch.delitem(sys.modules, "lottery.rigor", raising=False)
    import importlib
    importlib.import_module("lottery.rigor")
    assert TestClient(api_app.app).get("/api/rigor/randomness").status_code == 200
    assert TestClient(api_app.app).get("/api/rigor/randomness").status_code == 200


def test_unknown_strategy_rejected():
    """未知策略须显式 400，不得静默返回某个默认策略的结论。"""
    from fastapi.testclient import TestClient
    from lottery.api_app import app
    client = TestClient(app)
    r = client.get("/api/rigor/signal?strategy=definitely_not_a_strategy")
    assert r.status_code == 400
    assert "supported" in r.json()


def test_expected_value_is_negative():
    """期望值分析必须体现彩票是负期望游戏（数学事实）。"""
    from lottery.api_app import ticket_expected_prize, TICKET_PRICE
    ev = ticket_expected_prize()
    assert ev["expected_prize_per_ticket"] < TICKET_PRICE, "单注期望必须低于票价 2 元"
    assert 0 < ev["hit_any_prize_probability"] < 0.2, "中奖概率应约为 6~7%"
