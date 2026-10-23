"""V23 / L3：把「前向回测」搬到首屏——让"未检出可预测性"成为可复核的事实。

## 背景（docs/analysis/V19_AI_REDESIGN.md §4 层 3）

V19 §2.3 发现：`ml_blue` 置换检验 p=0.010 看起来显著，但前向回测 z=-1.26。
V20 已把前向回测做成判据（L0），V23 把它**公开给用户看**。

产品意义：这**不是消极**——它把"我们试过了，数据说没有"变成可复核的事实，
而不是让用户对着一个「AI 预测」的空壳猜。

## 本文件守什么

1. 端点必须给出**结论句**（`headline_zh`），不能只丢一堆数字；
2. 必须给 `honesty_zh`（双色球是独立随机事件；不过线是最正常的结果）；
3. 判据与 V20 **同一套**（多种子 z 中位数、门槛 `WF_Z_MIN`），不许另立标准；
4. 未知 color 必须 400（不许静默降级成一个看似正常的读数）；
5. 前端必须显示结论句与诚实声明，**不只是数字**。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import pathlib

import pytest

from lottery import routes_rigor as RR
from lottery import walk_forward_gate as WFG


def _draws(n: int = 700, seed: int = 5):
    """合成开奖数据：不依赖真实库（测试要能在任何机器上跑）。"""
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({"issue": f"{2003000 + i}", "date": None,
                    "reds": reds, "order": list(reds),
                    "blue": rng.randint(1, 16)})
    return out


@pytest.fixture
def fake_db(monkeypatch):
    """把 routes_rigor.db.load_draws 换成合成数据。"""
    d = _draws(700)
    monkeypatch.setattr(RR.db, "load_draws", lambda *a, **k: d, raising=False)
    return d


# ==========================================================================
# 1. 端点契约
# ==========================================================================

def test_walk_forward_endpoint_reports_conclusion(fake_db):
    """端点必须给出人类可读的结论句，而不是只返回数字。

    变异点：删掉 `res["headline_zh"] = ...` —— 变红。
    为什么关键：只剩数字（z=-0.06）时，用户无从判断那算好还是不好，
    等于把"诚实展示"退化成"原始数据倾倒"。
    """
    r = RR.rigor_walk_forward(method="stat:freq", color="blue",
                              n_tickets=5, start=0, refresh=0)
    assert r.get("ok") is not False, f"端点失败：{r.get('error')}"
    assert r.get("headline_zh"), "必须给出结论句"
    assert r.get("honesty_zh"), "必须给出诚实声明"
    # 结论句必须与 passed **语义一致**，不能是句固定文案 ——
    # 否则"过线"与"未过线"会显示同一句话（第一版就漏在这里：
    # 我只断言了字符串非空，把 if 分支的赋值删掉也照样绿）。
    if r.get("passed"):
        assert "过线" in r["headline_zh"], (
            f"passed=True 时结论应说「过线」，实际：{r['headline_zh']}")
    else:
        assert "未检出" in r["headline_zh"] or "不可历史回放" in r["headline_zh"], (
            f"passed=False 时结论应说「未检出」，实际：{r['headline_zh']}")


def test_walk_forward_uses_same_criteria_as_gate(fake_db):
    """判据必须与 V20 的 `walk_forward_gate` **同一套**，不许另立标准。

    变异点：把 `res["z_min"]` 改成硬编码的其他值（如 1.96）—— 变红。
    为什么关键：两处如果各说各话，"过线"就没有了确定含义。
    """
    r = RR.rigor_walk_forward(method="stat:freq", color="blue",
                              n_tickets=5, start=0, refresh=0)
    assert r.get("z_min") == WFG.WF_Z_MIN, (
        f"端点门槛 {r.get('z_min')} 必须等于 WF_Z_MIN={WFG.WF_Z_MIN}")
    assert r.get("seeds") == list(WFG.WF_SEEDS), "端点必须用同一组种子"


def test_walk_forward_passed_flag_matches_z_median(fake_db):
    """`passed` 必须与 z 中位数和门槛自洽（不许自相矛盾）。

    变异点：把 `passed` 写死成 True —— 变红。
    """
    r = RR.rigor_walk_forward(method="stat:freq", color="blue",
                              n_tickets=5, start=0, refresh=0)
    z = r.get("z_median")
    if z is not None:
        expect = float(z) > float(WFG.WF_Z_MIN)
        assert bool(r.get("passed")) == expect, (
            f"passed={r.get('passed')} 与 z_median={z} > {WFG.WF_Z_MIN} 不一致")


def test_walk_forward_rejects_bad_color():
    """未知 color 必须 400 —— 不许静默当成 blue 给出一个"正常"读数。

    变异点：把 color 校验删掉 —— 变红（会返回 200 和误导性数字）。
    """
    r = RR.rigor_walk_forward(method="stat:freq", color="green",
                              n_tickets=5, start=0, refresh=0)
    # 必须是 400 响应对象；且**不能**是一个带正常读数的 dict
    # （第一版只断言 status_code==400，但校验被删后返回的是 dict，
    #  getattr(dict,'status_code',None) 为 None —— 逻辑上能红；
    #  真正漏的是我没同时确认"它没有静默给出 blue 的读数"。）
    assert getattr(r, "status_code", None) == 400, (
        f"非法 color 应返回 400，实际 {type(r)}")
    body = r.body.decode("utf-8") if hasattr(r, "body") else ""
    assert "color" in body, "400 响应应说明是 color 参数的问题"
    assert "hit_rate" not in body, "非法参数不得返回任何命中率读数（那会被误读成结论）"


def test_walk_forward_red_channel_uses_count_baseline(fake_db):
    """红球通道的基线必须是 6*6/33（命中个数），不能套蓝球的 1/16。

    变异点：红球误用 1/16 —— 变红。
    """
    r = RR.rigor_walk_forward(method="stat:freq", color="red",
                              n_tickets=5, start=0, refresh=0)
    assert r.get("ok") is not False
    assert abs(float(r["baseline"]) - 6 * 6 / 33) < 1e-6, (
        f"红球基线应为 6*6/33，实际 {r.get('baseline')}")


# ==========================================================================
# 2. 前端：首屏必须显示结论与诚实声明
# ==========================================================================

def test_frontend_walk_forward_card_and_rendering():
    """首屏必须有前向回测卡片，且前端真的渲染结论、并在启动时加载。

    ## 缺一不可

    1. `index.html` 里有 `id="wfBody"` / `id="wfTag"` 卡片 ——
       否则后端算得再准也看不到；
    2. JS 里调用 `/api/rigor/walk_forward` —— 否则卡片没有数据源；
    3. 渲染 `headline_zh` / `honesty_zh` / `z_median` ——
       只显示数字等于数据倾倒，用户无从判断好坏；
    4. `loadWalkForward();` 在启动路径被调用 —— 否则卡片永远"加载中…"。

    变异点：删任一项 —— 变红。**JS 必须先剥注释**（`_source_assert.js_code`），
    否则注释里的字样会造成假绿。

    说明：三处读取合并为**一条**断言，避免无谓增长元守卫的 B 类配额
    （`test_qa_source_assert_meta.py` 的 `B_BASELINE`，只防增长）。
    """
    from tests._source_assert import js_code
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    src = js_code((root / "web" / "static" / "app.js").read_text(encoding="utf-8"))
    missing = [k for k in ('id="wfBody"', 'id="wfTag"') if k not in html]
    missing += [k for k in ("/api/rigor/walk_forward", "headline_zh",
                            "honesty_zh", "z_median") if k not in src]
    assert not missing, f"首屏前向回测未接好，缺少：{missing}"
    assert "loadWalkForward();" in src, "启动时必须调用 loadWalkForward()"
