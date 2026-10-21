"""V10-04b 测试：出号通道接线 + 前端默认值 + 报表口径（R3 P0-4 / R1 P1-4）。

覆盖三块交付：

1. ``engine.predict_next`` 的出号通道一律读 ``methods.resolve_channels()``
   （``config.CHANNELS`` 快照），**不再**直读 ``config.LLM_ONLY_OUTPUT``：
   ``llm_can_pick=False`` 时即使兼容开关 ``LOTT_LLM_ONLY_OUTPUT=1`` 也绝不走
   LLM 出号；``only`` / ``mix`` / ``off`` 三种模式行为各自正确。
2. 前端默认值：``web/index.html`` 的 ``cfgLlm`` 不得硬编码 ``checked``、
   ``app.js`` 必须有 localStorage 版本迁移、``?v=`` 必须同步升级。
3. 报表口径：``evaluate.method_breakdown()`` 按族分列，**不得**出现跨族
   加权平均（``eval_results`` 的混合标量不再作为方法战绩）。

规格来源：``docs/V10_IMPROVEMENT_PLAN.md`` §2.1.3 / §3.5.2，
评审依据：``docs/reviews/R3_AI_REVIEW.md`` P0-4、``docs/reviews/R1_ARCHITECTURE_REVIEW.md`` P1-4。
"""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
APP_JS = WEB_DIR / "static" / "app.js"

def _js_code() -> str:
    """剥离注释后的 app.js。

    ⚠️ 为什么需要（2026-10-21）：本文件有断言用「字符串是否出现」来验证前端行为。
    若该字符串**同时出现在注释里**，删掉实现后断言仍然通过 —— 典型假绿。
    实测发现本文件 ``\u201c照常出号\u201d``、``\u201c不受系统配额限制\u201d`` 两处在注释中也有，
    故改用本函数。新增此类断言一律用它。
    """
    import re as _re
    js = APP_JS.read_text(encoding="utf-8")
    js = _re.sub(r"/\*.*?\*/", "", js, flags=_re.S)
    js = _re.sub(r"(?<![:\"'])\s*//[^\n]*", "", js)
    return js


# ---------------------------------------------------------------- 公共构造器

def _draws(n: int = 400, seed: int = 20260101) -> list:
    """合成历史开奖（升序、结构同 db.load_draws()）。"""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rnd.sample(range(1, 34), 6))
        out.append({
            "issue": f"{2026001 + i}",
            "date": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
            "reds": reds,
            "blue": rnd.randint(1, 16),
        })
    return out


def _llm_ticket(i: int, blue: int) -> dict:
    """构造一个合法的 LLM 候选（method 以 llm: 开头，且不超过 n_tickets 注）。"""
    return {
        "reds": sorted((i + j) % 33 + 1 for j in range(6)),
        "blue": blue,
        "method": "llm:fake-model",
        "confidence": 60 - i,
        "reasoning": "结构均衡",
    }


@pytest.fixture()
def env(isolated_env, monkeypatch):
    """隔离环境 + 方法开关复位（不依赖机器上真实的 .env / methods_config.json）。"""
    from lottery import config, db

    monkeypatch.setenv("LOTT_ML_ENABLED", "0")
    monkeypatch.setattr(config, "METHODS_RAW", "")
    monkeypatch.setattr(config, "METHOD_MODE", "production")
    monkeypatch.setattr(config, "METHODS_SPEC", {"mode": "all", "tokens": set()})
    monkeypatch.setattr(config, "CHANNELS",
                        _channels("off", llm_can_pick=False, families_llm=True))
    db.close()
    return config


def _channels(llm_output_mode: str, *, llm_can_pick: bool,
              families_llm: bool = True, mode: str = "production") -> dict:
    """与 ``methods.resolve_channels()`` 同 schema 的通道快照。"""
    return {
        "spec_raw": "stat,ml,uniform,llm",
        "source": "default",
        "mode": mode,
        "families": {"stat": True, "blend": False, "llm": families_llm,
                     "ml": True, "uniform": True},
        "llm_output_mode": llm_output_mode,
        "llm_can_pick": llm_can_pick,
        "research_arm": {"stat": True, "llm_probe": mode == "research" and families_llm},
        "fingerprint": "0123456789abcdef",
    }


def _with_llm(monkeypatch, produced):
    """把 LLM 通道打桩为固定产出（不发起真实网络调用）。"""
    from lottery import engine as E
    monkeypatch.setattr(E, "llm_tickets", lambda *a, **k: [dict(t) for t in produced])


# ============================================================ 1) engine 出号通道

def test_llm_can_pick_false_never_picks_even_if_legacy_switch_on(env, monkeypatch):
    """核心：``llm_can_pick=False`` 时即使 ``LOTT_LLM_ONLY_OUTPUT=1`` 也不走 LLM 出号。

    R3 P0-4：出号判据必须只有一处（resolve_channels）。族被关掉（或研究模式/
    出号模式 off）时，兼容开关 1 不得再"偷偷"把最终注数压成 LLM 实际产出。
    """
    from lottery import engine as E
    _with_llm(monkeypatch, [_llm_ticket(i * 3 + 1, b)
                            for i, b in enumerate([1, 9, 10, 2])])
    monkeypatch.setenv("LOTT_LLM_ONLY_OUTPUT", "1")     # 旧开关显式打开
    monkeypatch.setattr(env, "LLM_ONLY_OUTPUT", True)   # 连 config 也一起打开
    # 快照：LLM 族关掉 → llm_can_pick=False
    monkeypatch.setattr(env, "CHANNELS",
                        _channels("off", llm_can_pick=False, families_llm=False))

    res = E.predict_next(_draws(), use_llm=True, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(1))

    assert res["llm_only_output"] is False
    assert res["actual_tickets"] == 10, "应回到「补齐到请求注数」的默认路径"
    assert res["shortfall_reason"] is None
    assert res["channels"]["llm_can_pick"] is False
    assert res["channels"]["llm_output_mode"] == "off"


def test_only_mode_outputs_exact_llm_count(env, monkeypatch):
    """``only`` 模式：最终注数 = LLM 实际产出注数（不补齐）。"""
    from lottery import engine as E
    _with_llm(monkeypatch, [_llm_ticket(i * 3 + 1, b)
                            for i, b in enumerate([1, 9, 10, 2])])
    monkeypatch.setattr(env, "CHANNELS", _channels("only", llm_can_pick=True))

    res = E.predict_next(_draws(), use_llm=True, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(2))

    assert res["llm_only_output"] is True
    assert res["actual_tickets"] == 4
    assert res["requested_tickets"] == 10
    assert res["shortfall_reason"] == "llm_only_actual_count"
    assert all(t["method"].startswith("llm:") for t in res["tickets"])
    assert res["channels"]["llm_output_mode"] == "only"


def test_mix_mode_pads_with_stat_tickets(env, monkeypatch):
    """``mix`` 模式：LLM 与 stat/ml 混合后补齐到 N_TICKETS（不是"只出 LLM 注"）。"""
    from lottery import engine as E
    _with_llm(monkeypatch, [_llm_ticket(i * 3 + 1, b)
                            for i, b in enumerate([1, 9, 10, 2])])
    monkeypatch.setattr(env, "CHANNELS", _channels("mix", llm_can_pick=True))

    res = E.predict_next(_draws(), use_llm=True, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(3))

    assert res["llm_only_output"] is False
    assert res["actual_tickets"] == 10
    assert res["shortfall_reason"] is None
    assert any(not t["method"].startswith("llm:") for t in res["tickets"]), \
        "mix 模式必须混入统计候选，不得退化成 only"
    assert res["channels"]["llm_output_mode"] == "mix"


def test_off_mode_uses_deterministic_path(env, monkeypatch):
    """``off`` 模式：LLM 只提供候选，最终出号仍按确定性排序补齐到 N_TICKETS。"""
    from lottery import engine as E
    _with_llm(monkeypatch, [_llm_ticket(i * 3 + 1, b)
                            for i, b in enumerate([1, 9, 10, 2])])
    monkeypatch.setattr(env, "CHANNELS", _channels("off", llm_can_pick=False))

    res = E.predict_next(_draws(), use_llm=True, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(4))

    assert res["llm_only_output"] is False
    assert res["actual_tickets"] == 10
    assert res["channels"]["llm_output_mode"] == "off"
    assert res["channels"]["llm_can_pick"] is False


def test_research_mode_llm_cannot_pick(env, monkeypatch):
    """research 模式：families.llm=True 但 ``llm_can_pick=False`` → 不走 LLM 出号。"""
    from lottery import methods as METH

    absent = env.DATA_DIR / "absent_methods_config.json"
    monkeypatch.setattr(env, "METHODS_CONFIG_FILE", absent)
    monkeypatch.setattr(METH, "_methods_config_file", lambda: absent)
    monkeypatch.setenv("LOTT_METHOD_MODE", "research")
    monkeypatch.setenv("LOTT_METHODS", "stat,ml,uniform,llm")
    monkeypatch.delenv("LOTT_LLM_ONLY_OUTPUT", raising=False)
    ch = METH.resolve_channels()
    assert ch["families"]["llm"] is True
    assert ch["mode"] == "research"
    assert ch["llm_can_pick"] is False

    from lottery import engine as E
    _with_llm(monkeypatch, [_llm_ticket(i * 3 + 1, b)
                            for i, b in enumerate([1, 9, 10, 2])])
    monkeypatch.setattr(env, "CHANNELS", ch)
    res = E.predict_next(_draws(), use_llm=True, use_ml=False, n_tickets=10,
                         persist=False, rng=random.Random(5))
    assert res["llm_only_output"] is False
    assert res["actual_tickets"] == 10


def test_engine_does_not_read_config_llm_only_output(monkeypatch):
    """静态闸门：``engine.py`` 不得再读 ``config.LLM_ONLY_OUTPUT``。

    R3 P0-4 / R1 P0-1：第 7 套开关的根源就是"每个模块各判一次"。
    出号判据只能来自 ``methods.resolve_channels()`` 快照。
    """
    src = (Path(__file__).resolve().parent.parent / "lottery" / "engine.py").read_text(
        encoding="utf-8")
    # 只看"真代码行"：去掉注释与文档字符串说明（说明文字里提到这个开关是合法的）
    code_lines = []
    in_doc = False
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        code_lines.append(line.split("#", 1)[0])
    code = "\n".join(code_lines)
    assert "LLM_ONLY_OUTPUT" not in code, \
        "engine.py 仍在直读 config.LLM_ONLY_OUTPUT，出号口径会与 resolve_channels 漂移"
    assert "channels_snapshot(" in code
    assert "llm_can_pick" in code


def test_channels_snapshot_prefers_config_snapshot(env, monkeypatch):
    """``channels_snapshot()`` 优先返回 ``config.CHANNELS`` 冻结快照。"""
    from lottery import engine as E
    snap = _channels("mix", llm_can_pick=True)
    monkeypatch.setattr(env, "CHANNELS", snap)
    assert E.channels_snapshot() is snap


def test_channels_snapshot_falls_back_when_missing(env, monkeypatch):
    """快照缺失（旧配置/被清空）时现算 ``resolve_channels()``，不得 KeyError。"""
    from lottery import engine as E, methods as METH
    monkeypatch.setattr(env, "CHANNELS", {})
    monkeypatch.setattr(env, "METHODS_CONFIG_FILE", env.DATA_DIR / "absent.json")
    monkeypatch.setattr(METH, "_methods_config_file", lambda: env.DATA_DIR / "absent.json")
    monkeypatch.delenv("LOTT_LLM_ONLY_OUTPUT", raising=False)
    monkeypatch.setenv("LOTT_METHODS", "stat,ml,uniform")
    monkeypatch.setenv("LOTT_METHOD_MODE", "production")
    ch = E.channels_snapshot()
    assert ch["llm_can_pick"] is False
    assert ch["fingerprint"]


# ============================================================ 2) 前端默认值

def test_cfgllm_checkbox_not_hardcoded_checked():
    """``web/index.html`` 的 ``cfgLlm`` 不得硬编码 ``checked``（R1 P1-6 / §2.1.3）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"<input[^>]*id=\"cfgLlm\"[^>]*>", html)
    assert m, "index.html 找不到 cfgLlm 复选框"
    tag = m.group(0)
    assert "checked" not in tag, (
        "cfgLlm 仍硬编码 checked：会把后端 V10 默认值（llm_output_mode=off）吃掉")
    assert "type=\"checkbox\"" in tag


def test_frontend_has_localstorage_version_migration():
    """``app.js`` 必须有一次性默认值迁移（旧 ``localStorage.cfgLlm`` 会吃掉新默认值）。"""
    js = _js_code()   # 剥离注释：llm_can_pick 等在注释里也出现
    assert "cfgLlmVersion" in js, "缺少 localStorage 默认值版本号，老用户旧值不会被迁移"
    assert "migrateCfgDefaults" in js
    assert "removeItem(\"cfgLlm\")" in js
    # 默认值必须来自后端 channels，而不是前端自己推
    assert "applyChannelDefaults" in js
    assert "llm_can_pick" in js
    # 迁移必须在读取 cfgLlm 之前执行（initConfig 内先调用，顺序不能反）
    init = re.search(r"function initConfig\(\)\s*\{(.*?)\n\}\)\(\);", js, re.S)
    assert init, "找不到 initConfig 立即执行块"
    body = init.group(1)
    assert body.index("migrateCfgDefaults()") < body.index("cfgLlm"), \
        "迁移必须先于读取旧值，否则旧值仍会生效"


def test_asset_cache_busters_bumped_together():
    """``?v=`` 必须已升到 >= 20261018.4 且 app.js / style.css 版本一致。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    m_app = re.search(r"app\.js\?v=(\d{8})\.(\d+)", html)
    m_css = re.search(r"style\.css\?v=(\d{8})\.(\d+)", html)
    assert m_app and m_css
    app_ver = (int(m_app.group(1)), int(m_app.group(2)))
    css_ver = (int(m_css.group(1)), int(m_css.group(2)))
    assert app_ver == css_ver, "app.js 与 style.css 的 ?v= 必须同步升版本"
    assert app_ver >= (20261018, 4), f"V10-04b 必须升 ?v=，实际 {app_ver}"


def test_api_info_exposes_channels_for_frontend_default():
    """/api/info 必须下发 channels（前端据此回填 cfgLlm，而不是硬编码）。"""
    from lottery.api_app import app_info
    info = app_info()
    ch = info.get("channels") or {}
    assert "llm_can_pick" in ch and isinstance(ch["llm_can_pick"], bool)
    assert "llm_output_mode" in ch
    assert "fingerprint" in ch


# ============================================================ 3) 报表口径（分列）

def _miss_reds(draw: dict) -> list:
    """取 6 个**完全不命中**该期开奖的号码（构造红球命中 = 0 的对照注）。"""
    pool = [x for x in range(1, 34) if x not in set(draw["reds"])]
    return sorted(pool[:6])


def _hit_reds(draw: dict, k: int = 3) -> list:
    """取 6 个号码，其中恰好 k 个命中该期开奖（用来造"红球命中 = k"）。"""
    rest = [x for x in range(1, 34) if x not in set(draw["reds"])]
    return sorted(list(draw["reds"][:k]) + rest[:6 - k])


def _miss_ticket(draw: dict, method: str) -> dict:
    """红球 0 命中、蓝球必不中的候选注（带 save_predictions 需要的 confidence）。"""
    return {"reds": _miss_reds(draw),
            "blue": (draw["blue"] % 16) + 1,
            "method": method, "confidence": 50.0}


def _hit_ticket(draw: dict, method: str, k: int = 3) -> dict:
    """红球命中 k 个、蓝球必不中的候选注。"""
    return {"reds": _hit_reds(draw, k),
            "blue": (draw["blue"] % 16) + 1,
            "method": method, "confidence": 50.0}


def _seed_predictions(issue: str, tickets: list) -> None:
    from lottery import db
    db.save_predictions(issue, tickets)


def test_method_breakdown_does_not_mix_llm_and_stat(seeded_db):
    """分列：llm 与 stat 命中率差异明显时，各族各自为政，**不出现加权平均**。

    构造：同一期里 llm 注红球命中 0，stat 注红球命中 3。
    混合平均 = 1.5（旧 ``int()`` 还会截成 1）；分列后 llm=0.0、stat=3.0。
    """
    from lottery import db, evaluate

    draws = seeded_db(n=40)
    draw = draws[-1]
    issue = draw["issue"]

    llm_tickets = [_miss_ticket(draw, "llm:fake")]        # 红球 0 命中
    stat_tickets = [_hit_ticket(draw, "stat:freq", 3)]    # 红球命中 3

    db.save_eval_details(issue, "llm:fake", llm_tickets, draw)
    db.save_eval_details(issue, "stat:freq", stat_tickets, draw)

    rep = evaluate.method_breakdown()
    fams = rep["families"]
    assert set(fams) >= {"llm", "stat"}
    assert fams["llm"]["red_hits_mean"] == 0.0
    assert fams["stat"]["red_hits_mean"] == 3.0
    # 混合平均 1.5 不得出现在任何族里，也不得有跨族合计字段
    for key in ("all", "total", "overall", "weighted", "mixed"):
        assert key not in rep, f"method_breakdown 不得返回跨族 {key} 字段"
    assert "avg_red_hits" not in rep
    assert "不得相加" in rep["note"]


def test_method_breakdown_has_no_cross_family_average(seeded_db):
    """结构闸门：``families`` 里每一块都必须带自己的 n_tickets，且没有合计行。"""
    from lottery import db, evaluate

    draws = seeded_db(n=30)
    draw = draws[-1]
    tickets_llm = [_miss_ticket(draw, "llm:m1")]
    tickets_ml = [_miss_ticket(draw, "ml")]
    db.save_eval_details(draw["issue"], "llm:m1", tickets_llm, draw)
    db.save_eval_details(draw["issue"], "ml", tickets_ml, draw)

    rep = evaluate.method_breakdown()
    assert rep["n_tickets"] == 2
    assert rep["n_issues"] == 1
    for fam, block in rep["families"].items():
        assert block["n_tickets"] >= 1, f"{fam} 缺少自身注数"
        assert block["methods"], f"{fam} 缺少来源方法"
    # 族间注数之和 == 总数 是"聚合事实"，但不得提供"平均"合成行
    assert sum(b["n_tickets"] for b in rep["families"].values()) == rep["n_tickets"]


def test_online_check_summary_is_landed_per_family(seeded_db):
    """``online_check`` 的 summary 不再给混合 avg_red_hits，改为 by_family 分列。"""
    from lottery import db, evaluate

    draws = seeded_db(n=40)
    draw = draws[-1]
    issue = draw["issue"]
    db.save_predictions(issue, [
        _miss_ticket(draw, "llm:fake"),                  # 红球 0 命中
        _hit_ticket(draw, "stat:freq", 3),               # 红球命中 3
    ])

    res = evaluate.online_check()
    summary = res["summary"]
    assert summary["checked"] >= 1
    assert summary["scope"] == "portfolio"
    assert "avg_red_hits" not in summary, "混合口径的 avg_red_hits 必须移除"
    assert "blue_hit_rate" not in summary, "混合口径的 blue_hit_rate 必须移除"
    assert "by_family" in summary
    assert summary["by_family"]["llm"]["red_hits_mean"] == 0.0
    assert summary["by_family"]["stat"]["red_hits_mean"] == 3.0
    assert "不得相加" in summary["mixed_note"] or "分列" in summary["mixed_note"]


def test_eval_results_keeps_float_means(seeded_db):
    """``eval_results`` 不再 ``int()`` 截断：1.5 必须存成 1.5，不是 1（R1 P1-4）。"""
    from lottery import db, evaluate

    draws = seeded_db(n=40)
    draw = draws[-1]
    issue = draw["issue"]
    db.save_predictions(issue, [
        _miss_ticket(draw, "llm:fake"),                  # 红球命中 0
        _hit_ticket(draw, "stat:freq", 3),               # 红球命中 3 → 两注均值 1.5
    ])

    evaluate.online_check()
    row = [r for r in db.load_eval() if r["issue"] == issue][0]
    assert float(row["red_hits"]) == pytest.approx(1.5), \
        f"混合均值应保留浮点 1.5，实际 {row['red_hits']!r}"


def test_eval_by_method_endpoint_shape(seeded_db):
    """/api/eval/by-method 返回分列结构（族 / method 两级）。"""
    from fastapi.testclient import TestClient
    from lottery import db
    from lottery.api_app import app

    draws = seeded_db(n=30)
    draw = draws[-1]
    db.save_eval_details(draw["issue"], "llm:m1", [_miss_ticket(draw, "llm:m1")], draw)

    r = TestClient(app).get("/api/eval/by-method?limit_issues=50")
    assert r.status_code == 200
    body = r.json()
    assert "families" in body and "methods" in body
    assert body["families"]["llm"]["n_tickets"] >= 1
    assert body["methods"][0]["method"] == "llm:m1"
    assert body["methods"][0]["family"] == "llm"
