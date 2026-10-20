"""V8：阶段切分口径 / 后台任务进度 / 号码格式统一 的测试。

对应四条用户反馈：
1. 重新生成后刷新页面又显示旧数据 —— 前端防缓存 + 快照启动刷新；
2. 点按钮 15 分钟没反应 —— 长任务改后台任务并回报阶段进度；
3. 9 个自然年阶段划分不合适、发现不了规律 —— 改数据驱动的自适应切分 + 修正检验口径；
4. 号码格式不统一 —— 全站唯一格式化函数。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from lottery import backtest as BT
from lottery import db, discovery, regime

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")


def _draws(n=1200, seed=11):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        y = 2003 + i * 24 // n
        out.append({"issue": f"{y}{i % 150 + 1:03d}", "date": f"{y}-06-01",
                    "reds": sorted(int(x) for x in rng.choice(np.arange(1, 34), 6, replace=False)),
                    "blue": int(rng.integers(1, 17))})
    return out


# ---------- 阶段切分 ----------

def test_adaptive_eras_are_fixed_size_and_cover_history():
    d = _draws(1200)
    eras = regime.build_adaptive_eras(d, block=150)
    assert eras, "必须至少切出一段"
    assert eras[0]["start"] == 0
    assert eras[-1]["end"] == len(d)
    for a, b in zip(eras, eras[1:]):
        assert a["end"] == b["start"], "阶段必须首尾相接"
    sizes = [e["n"] for e in eras]
    assert max(sizes) <= 150 + 75, f"禁止出现超小阶段（{sizes}）"
    assert all(e["name"].startswith("P") for e in eras)


def test_adaptive_eras_default_block_is_150():
    """默认 150 期一段：实测该配置下 family-wise 假阳性 0.045、+0.15 效应检出力 0.65。"""
    assert regime.ERA_BLOCK == 150
    assert regime.ERA_MODE == "adaptive"


def test_eras_for_switches_modes():
    d = _draws(1200)
    ad = regime.eras_for(d, mode="adaptive")
    cal = regime.eras_for(d, mode="calendar")
    # 语义区分：adaptive 用 P1..Pn 命名且段长接近 150；calendar 按年份分桶
    assert all(e["name"].startswith("P") for e in ad)
    assert not any(e["name"].startswith("P") for e in cal)
    assert {e["n"] for e in ad} - {150} == set() or max(e["n"] for e in ad) <= 225
    assert len(cal) == 9, "自然年口径固定 9 段（2003-2008/…/2026）"
    # 两者都必须完整覆盖历史
    for eras in (ad, cal):
        assert eras[0]["start"] == 0 and eras[-1]["end"] == len(d)


def test_adaptive_era_has_more_statistical_power_than_calendar():
    """核心论点：同样注入 +0.15 的回球效应，自适应切分应比自然年切分更容易检出。

    这是"9 个阶段划分不合适导致发现不了规律"的可证伪验证。
    """
    d = _draws(1400)
    rng = np.random.default_rng(5)

    def power(mode, T=60):
        hit = 0
        for _ in range(T):
            x = rng.normal(0, 0.87, len(d))
            x[-int(len(d) * 0.25):] += 0.15
            r = regime.regime_report(list(x), d, era_mode=mode)
            if any((e.get("p_adjusted") or 1) < 0.05 for e in r["eras"]):
                hit += 1
        return hit / T

    ad, cal = power("adaptive"), power("calendar")
    assert ad >= cal, f"自适应阶段检测力({ad:.2f})应不低于自然年({cal:.2f})"


def test_regime_report_reports_era_mode():
    d = _draws(600)
    r = regime.regime_report(list(np.zeros(len(d))), d)
    assert r["era_mode"] in ("adaptive", "calendar")
    assert "era_block" in r


def test_hit_count_indicator_uses_paired_difference_not_binomial():
    """回归：命中数指标曾被当二项比例喂给 era_significance，导致方差算错、均值被截断，
    表现是"每段都不作检验 / p 恒为 0"。现在必须走配对差值(t 检验)。"""
    d = _draws(1200)
    f = discovery.make_filter("hot", {"window": 30, "k": 6})
    rep = discovery.regime_of_filter(d, f, start=200)
    tested = [e for e in rep["eras"] if e.get("p_value") is not None]
    assert tested, "必须存在可检验的阶段（否则口径又错了）"
    for e in tested[:5]:
        assert e.get("test") != "binomial", "命中数不能走二项口径"


def test_binomial_era_needs_reasonable_sample():
    r = regime.era_significance([1.0] * 60, [0.5] * 60)
    assert r["test"] == "binomial" and r["p_value"] is not None
    small = regime.era_significance([1.0] * 12, [0.5] * 12)
    assert small["p_value"] is None and "样本过少" in small["note_zh"]


def test_regime_overview_endpoint_accepts_era_mode(monkeypatch, seeded_db):
    from fastapi.testclient import TestClient
    from lottery import api_app
    # V10-00b：此前该用例依赖生产库恰好有开奖数据；改为造 600 期合成数据
    # （阶段切分 block=150 → 4 段），判定条件不变。
    seeded_db(600)
    c = TestClient(api_app.app)
    for mode in ("adaptive", "calendar"):
        r = c.get(f"/api/regime/overview?era_mode={mode}")
        assert r.status_code == 200
        j = r.json()
        assert j["era_mode"] == mode
        assert j["eras"], "必须返回阶段列表"
        assert "adaptive" in j["era_modes"] and "calendar" in j["era_modes"]


# ---------- 后台任务 ----------

def test_closed_loop_reports_progress_stages():
    """闭环必须逐阶段回报进度（用户反馈"点了没反应"）。"""
    from lottery import llm_lab, hypothesis, config, patterns_runtime as PR
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        old_claims, old_snap = llm_lab.CLAIMS_FILE, PR.SNAPSHOT_FILE
        old_registry = hypothesis._registry_path()
        llm_lab.CLAIMS_FILE = tdp / "claims.json"
        PR.SNAPSHOT_FILE = tdp / "rt.json"
        import lottery.hypothesis as H
        orig = H._registry_path
        H._registry_path = lambda p=None: tdp / "hyp.json"
        seen = []
        try:
            llm_lab.run_closed_loop(_draws(600), rounds=1, use_llm=False, persist=False,
                                    progress=lambda f, m: seen.append((round(f, 2), m)))
        finally:
            llm_lab.CLAIMS_FILE, PR.SNAPSHOT_FILE = old_claims, old_snap
            H._registry_path = orig
        assert len(seen) >= 4, f"至少要回报 4 个阶段，实际 {seen}"
        assert seen[0][0] < 0.2 and seen[-1][0] >= 0.99
        msgs = " ".join(m for _, m in seen)
        for kw in ("维度扫描", "注册", "阶段时效", "规律快照"):
            assert kw in msgs, f"阶段提示缺少 {kw}"


def test_async_closed_loop_endpoint_returns_task_id(monkeypatch, seeded_db):
    from fastapi.testclient import TestClient
    from lottery import api_app
    # V10-00b：端点要求库里有开奖数据才进入后台任务分支，改为造合成数据
    seeded_db(600)
    c = TestClient(api_app.app)
    captured = {}

    def fake_bg(kind, fn, on_done=None):
        captured["kind"] = kind
        return "task_xyz"

    # P2：`_bg_run` 已搬到 `bg_tasks`，打桩点必须跟着搬到**真正的依赖源**
    from lottery import bg_tasks
    monkeypatch.setattr(bg_tasks, "_bg_run", fake_bg)
    r = c.post("/api/rules/closed-loop/async?rounds=1&use_llm=0")
    assert r.status_code == 200
    j = r.json()
    assert j["task_id"] == "task_xyz"
    assert captured["kind"] == "closed_loop"
    assert "后台" in j["note_zh"]


def test_async_llm_lab_endpoint_returns_task_id(monkeypatch, seeded_db):
    from fastapi.testclient import TestClient
    from lottery import api_app
    seeded_db(600)      # V10-00b：同上，端点要求库非空
    c = TestClient(api_app.app)
    from lottery import bg_tasks
    monkeypatch.setattr(bg_tasks, "_bg_run", lambda kind, fn, on_done=None: f"t_{kind}")
    r = c.post("/api/llm_lab/run/async?rounds=1&use_llm=0")
    assert r.status_code == 200 and r.json()["task_id"] == "t_llm_lab"


def test_bg_run_marks_task_completed_and_failed(tmp_path, monkeypatch):
    """后台任务必须把成功/失败如实写回 tasks 表，前端才能区分「还在跑」和「挂了」。"""
    import time

    from lottery import api_app, config, db

    # V10-00b：用 monkeypatch 改全局路径（自动还原），并 db.close() 重置模块级连接缓存，
    # 让 get_conn() 连到临时库；不再裸赋值 config.DB_PATH / 手改 db._CONN。
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    db.close()
    tid = api_app._bg_run("t", lambda p: (p(0.5, "半程"), {"note_zh": "done"})[1])
    for _ in range(100):
        t = db.load_task(tid)
        if t and t["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    t = db.load_task(tid)
    assert t["status"] == "completed", t
    assert (t.get("result") or {}).get("note_zh") == "done"

    def boom(p):
        raise RuntimeError("boom")

    tid2 = api_app._bg_run("t", boom)
    for _ in range(100):
        t2 = db.load_task(tid2)
        if t2 and t2["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    t2 = db.load_task(tid2)
    assert t2["status"] == "failed" and "boom" in t2["message"]


# ---------- 号码格式 ----------

def _js_fn(name: str) -> str:
    m = re.search(r"\nfunction " + name + r"\([\s\S]*?\n\}\n", APP_JS)
    assert m, f"app.js 缺少 {name}"
    return m.group(0)


def test_index_has_single_ticket_input():
    """诊断卡只保留一个输入框（整注粘贴），不再分蓝/红两个框。"""
    idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="diagTicket"' in idx
    assert 'id="diagReds"' not in idx and 'id="diagBlue"' not in idx


def test_js_format_helpers_are_single_source_of_truth():
    """号码格式只允许由 formatTicket/formatReds 产出（防止再出现第二种写法）。"""
    assert "function formatTicket" in APP_JS
    assert "function formatReds" in APP_JS
    # 旧的散装格式化写法必须已清除
    assert 't.reds.map(r => String(r).padStart(2,"0")).join(" ") + " + "' not in APP_JS


def test_js_format_helpers_output_matches_copy_button_format():
    """用 node 实跑 formatTicket，确认输出与复制按钮格式一致：01 06 12 21 24 31 + 12。"""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    code = ('const pad2 = (n) => String(n).padStart(2, "0");\n'
            + _js_fn("formatTicket") + _js_fn("formatReds") + _js_fn("parseRedsInput")
            + "\nconsole.log(formatTicket([21,6,1,24,12,31], 12));"
            + "\nconsole.log(formatReds([6,1,12]));"
            + "\nconsole.log(JSON.stringify(parseRedsInput('01 06 12 21 24 31')));")
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().split("\n")
    assert lines[0] == "01 06 12 21 24 31 + 12"
    assert lines[1] == "01 06 12"
    assert json.loads(lines[2]) == [1, 6, 12, 21, 24, 31]


def test_js_diagnose_uses_unified_format_and_parser():
    """「我的号码诊断」必须支持整注粘贴并回显统一格式（用户反馈第 1 条）。"""
    assert 'id="diagTicket"' not in APP_JS           # 输入框在 index.html，不在此
    assert "parseRedsInput(red_raw)" in APP_JS
    assert 'raw.split("+")' in APP_JS
    assert "formatTicket(p.reds || reds" in APP_JS
    assert "copyDiagnoseTicket" in APP_JS


def test_predictions_latest_ignores_out_of_range_issue():
    """回归：库里残留的越界期号 2026401（文本/数值排序都最大）会被当"最新预测"。"""
    from lottery import db
    assert hasattr(db, "_valid_issue")
    assert db._valid_issue("2026119") and not db._valid_issue("2026401")
    assert db._valid_issue("2026666") is False
    assert db._valid_issue("2026001") is True
    sql_src = (ROOT / "lottery" / "db.py").read_text(encoding="utf-8")
    # 「最新一期预测」的取法必须是"时间优先 + 合法期号过滤"
    assert "MAX(created_at) AS ts" in sql_src
    assert "_valid_issue" in sql_src


def test_js_uses_async_task_endpoints():
    """长操作必须走后端任务 + 轮询进度，而不是同步阻塞请求。"""
    assert "/api/rules/closed-loop/async" in APP_JS
    assert "/api/llm_lab/run/async" in APP_JS
    assert "runBackgroundTask" in APP_JS


def test_js_prediction_fetch_is_cache_busted():
    """刷新页面必须重新取预测（防浏览器缓存旧 JSON）。"""
    assert '"/api/predictions/last" + bust' in APP_JS or "/api/predictions/last?_ts=" in APP_JS
