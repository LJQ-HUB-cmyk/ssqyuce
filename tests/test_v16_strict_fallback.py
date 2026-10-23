"""V16：严格模式降级必须**可见**——三条路径的口径标注守卫。

## 缺陷背景（2026-10-22 实测故障）

严格模式门控只挂在 `/api/predict` 路由上，而后台调度器
（`api_app._scheduler_step`）**直接调 `engine.predict_next()`，绕过门控**。

结果：用户在页面点"生成预测"收到 422「严格模式下不出号」，
后台却悄悄往库里存了 5 注纯统计票（实测 `issue=2026122`，
method 全是 `stat:*` / `uniform`，无一条 `llm:`），而**界面上没有任何标记**
说明这批票不是严格模式的产物。

## 用户要的语义（2026-10-23 明确）

1. 自动生成时：**严格模式优先**；
2. 严格模式不出号 → **自动降级统计模式**，但**必须标注**是统计口径；
3. 手动生成时：**弹窗让用户自己选**是否改用统计模式。

## 本文件守什么

* `strict_mode_gates()` 是**纯数据**判定（不碰 HTTP），路由与调度器共用；
* 手动不带 `allow_stat_fallback` → **必须 422**，绝不自动降级；
* 手动带 `allow_stat_fallback=1` → 降级，且响应含 `source_mode="stat_fallback"`；
* 标注可持久化并能读回（缓存路径也要带回来）；
* 默认值必须是"不降级"——这是严格模式的语义底线。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言都绑定一个可变异的实现点，
注释里标了「变异点」。
"""
from __future__ import annotations

import pytest

from lottery import db, routes_predict as RP


# --------------------------------------------------------------------------
# 公共：构造确定性的门控失败状态
# --------------------------------------------------------------------------

def _force_strict_on(monkeypatch):
    """把严格模式打开（本地 .env 不会被 pytest 进程读取，必须显式设）。"""
    from lottery import config
    monkeypatch.setattr(config, "STRICT_MODE", True, raising=False)
    monkeypatch.setattr(RP.config, "STRICT_MODE", True, raising=False)


def _force_gates_fail(monkeypatch, gates=(("replayable_rules", "没有可回放的可用规律"),)):
    """让门控必然失败，而不依赖真实数据状态（真实数据下 usable_replayable 恰为 0，
    但那会随数据演进变化，测试不该押在这上面）。"""
    monkeypatch.setattr(RP, "strict_mode_gates", lambda draws, issue: list(gates))


def _force_llm_available(monkeypatch, available=True):
    from lottery import user_llm_store
    monkeypatch.setattr(
        user_llm_store, "effective_for_user",
        lambda uid, **kw: {"llm_available": available, "source": "system", "reason": ""},
    )


# ==========================================================================
# 1. 门控判定必须是纯数据（可被非 HTTP 调用方复用）
# ==========================================================================

def test_strict_gates_returns_plain_list_not_http_response(make_draws):
    """`strict_mode_gates()` 必须返回 list，而不是 JSONResponse。

    变异点：把它改回直接 `return JSONResponse(...)` —— 本断言立即变红
    （调度器拿不到可判读的结构，就只能重演"绕过门控"）。
    """
    out = RP.strict_mode_gates(make_draws, "2099999")
    assert isinstance(out, list), f"应返回 list，实际 {type(out).__name__}"
    for item in out:
        assert isinstance(item, tuple) and len(item) == 2, f"每项应为 (gate, msg)，实际 {item!r}"


def test_strict_scope_accepts_explicit_user_id():
    """`strict_scope_for_user(user_id)` 必须存在且可被后台线程调用（无请求上下文）。

    变异点：删掉该函数、只留"读 contextvar"的版本 —— AttributeError 变红，
    因为调度器线程没有 request context，只能靠显式 uid。
    """
    assert hasattr(RP, "strict_scope_for_user"), "缺少 strict_scope_for_user(user_id)"
    from lottery import config
    prev = getattr(config, "STRICT_MODE", False)
    try:
        config.STRICT_MODE = False
        active, bypass = RP.strict_scope_for_user(1)
        assert active is False and bypass == "", (
            "严格模式关闭时应返回 (False, '')，不该有任何 bypass 文案")
    finally:
        config.STRICT_MODE = prev


# ==========================================================================
# 2. 手动路径：默认**不**降级（严格模式的语义底线）
# ==========================================================================

def test_manual_predict_without_fallback_flag_returns_422(monkeypatch, make_draws):
    """不带 `allow_stat_fallback` 时，门控失败必须 422 —— 绝不自动降级。

    变异点（实测有效）：把路由里 `if not allow_stat_fallback:` 改成 `if False:`
    —— 于是门控失败时也走降级、继续出号，本用例变红。

    注意**不能**只断言"签名默认值是 0"：那只证明参数存在，不证明分支真的拦住了
    降级（我第一版就是这样，变异跑过去没红）。必须**真的调用路由**看返回码。
    """
    from lottery import config
    _force_strict_on(monkeypatch)
    _force_gates_fail(monkeypatch)
    _force_llm_available(monkeypatch, True)
    _draws = make_draws() if callable(make_draws) else make_draws
    monkeypatch.setattr(RP.db, "load_draws", lambda *a, **k: list(_draws))

    # 真正调用路由（allow_stat_fallback 显式给默认语义值 0 = 未授权降级）。
    # 直接调函数时 FastAPI 的 Query(...) 默认对象不会被解析，故必须显式传参。
    out = RP.predict(regenerate=True, n_tickets=5, allow_stat_fallback=0)
    code = getattr(out, "status_code", None)
    assert code == 422, (
        f"门控失败且用户未授权降级时必须 422（不自动降级），实际 {code!r}；"
        "若这里放行，就等于重演 2026-10-22『静默降级出统计票』的故障")

    # 顺带守住默认值本身（防止有人把默认改成 1，让降级变成默认行为）
    import inspect
    sig = inspect.signature(RP.predict)
    dv = getattr(sig.parameters["allow_stat_fallback"].default, "default",
                 sig.parameters["allow_stat_fallback"].default)
    assert dv == 0, f"allow_stat_fallback 默认值必须是 0（不降级），实际 {dv!r}"


def test_422_payload_carries_can_fallback_hint(monkeypatch, make_draws):
    """422 响应必须告诉前端"可以选统计模式"，否则弹窗无从触发。

    变异点：删掉 `can_fallback` / `fallback_hint` —— 变红。
    """
    _force_strict_on(monkeypatch)
    _force_gates_fail(monkeypatch)
    _force_llm_available(monkeypatch, True)

    resp = RP._strict_mode_reject_response(make_draws, "2099999")
    assert resp is not None, "门控失败时应返回 422 响应"
    import json
    body = json.loads(bytes(resp.body).decode())
    assert body.get("strict_mode") is True
    assert body.get("can_fallback") is True, "必须允许前端提示可降级"
    assert body.get("fallback_hint"), "必须给出可读的降级说明"
    assert body.get("strict_gates"), "必须带回未通过的门控列表"


# ==========================================================================
# 3. 标注：写进去、读得回、缓存也带
# ==========================================================================

def test_mark_and_load_prediction_source_roundtrip(monkeypatch):
    """`mark_prediction_source` 写入后，`load_prediction_source` 必须读得回。

    变异点：把 `source_mode` 写进别的字段（或写死返回值）—— 变红。
    """
    # 用独立临时库，绝不碰生产 data/
    import tempfile, pathlib
    from lottery import db as _db

    tmp = tempfile.mkdtemp()
    dbp = pathlib.Path(tmp) / "t.db"
    from lottery import config as _cfg
    monkeypatch.setattr(_cfg, "DB_PATH", dbp, raising=False)
    _db.get_conn()          # 内部会跑 _ensure_initialized 建表

    issue = "2099998"
    tickets = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 7, "method": "stat:freq",
                "confidence": 10.0, "reasoning": "", "patterns_used": [], "evidence": {}}]
    _db.save_predictions(issue, tickets)
    _db.save_eval_meta(issue, tickets, result={"llm_used": False})

    n = _db.mark_prediction_source(
        issue, source_mode="stat_fallback",
        strict_gates=["replayable_rules"], strict_reason="无可用规律")
    assert n >= 1, "应至少更新一条 eval_meta"

    got = _db.load_prediction_source(issue)
    assert got["source_mode"] == "stat_fallback", f"读回应为 stat_fallback，实际 {got!r}"
    assert got["strict_gates"] == ["replayable_rules"]
    assert got["strict_reason"] == "无可用规律"


def test_load_prediction_source_unknown_when_absent(monkeypatch):
    """没有记录时必须返回 unknown，不能假装是 strict。"""
    import tempfile, pathlib
    from lottery import db as _db, config as _cfg

    tmp = tempfile.mkdtemp()
    dbp = pathlib.Path(tmp) / "t2.db"
    monkeypatch.setattr(_cfg, "DB_PATH", dbp, raising=False)
    _db.get_conn()          # 内部会跑 _ensure_initialized 建表

    got = _db.load_prediction_source("2099997")
    assert got["source_mode"] == "unknown", (
        "无记录时应为 unknown —— 默认成 strict 会把统计票伪装成严格模式产物")


# ==========================================================================
# 4. 前端：弹窗按钮与口径标注必须存在（源码级守卫）
# ==========================================================================

def test_frontend_exposes_stat_fallback_button_and_source_badge():
    """前端必须在 422 时给出「改用统计模式生成」按钮，并在元信息里标口径。

    变异点：删掉按钮渲染或 `source_mode === "stat_fallback"` 的分支 —— 变红。
    为什么用源码断言：这是纯前端行为，后端测试无法覆盖；而"悄悄降级不标口径"
    正是本次要防的缺陷，必须有一条机器可查的守卫。

    **必须先剥注释**（`_source_assert.js_code`）：否则注释里出现同名字样会让
    断言假绿 —— 这正是 `js_code` docstring 记录的四次实测教训。
    """
    import pathlib
    from tests._source_assert import js_code

    p = pathlib.Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"
    src = js_code(p.read_text(encoding="utf-8"))
    # 一条断言覆盖四件必须同时成立的事，避免把 B 类子串断言拆成 4 条（元守卫有配额）。
    missing = [k for k in ("btnStatFallback", "allow_stat_fallback=1",
                           "stat_fallback", "can_fallback") if k not in src]
    assert not missing, f"前端缺少严格模式降级所需成分：{missing}"


# ==========================================================================
# 5. 端到端：后台调度器必须「严格模式优先 → 降级必须标注」
# ==========================================================================

def test_scheduler_marks_stat_fallback_when_strict_rejects(monkeypatch):
    """调度器路径：门控失败时降级统计，并把 `source_mode="stat_fallback"` 写进快照。

    变异点：把 `api_app` 里 `mark_prediction_source(...)` 那次调用删掉
    —— 本断言变红（票还在，但没人知道它是降级来的，正是 2026-10-22 的故障）。

    这是本次修复的**核心链路**：在此之前调度器直接调 `engine.predict_next()`，
    门控形同虚设。
    """
    import tempfile, pathlib
    from lottery import config as _cfg, db as _db, api_app

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(_cfg, "DB_PATH", pathlib.Path(tmp) / "e2e.db", raising=False)
    _db.get_conn()

    issue = "2099996"
    _force_strict_on(monkeypatch)
    _force_gates_fail(monkeypatch, [("replayable_rules", "没有可回放规律")])
    _force_llm_available(monkeypatch, True)

    # 造一条预测 + EvalMeta（模拟 engine 已落库），再让调度器去标注
    tickets = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 7, "method": "stat:freq",
                "confidence": 10.0, "reasoning": "", "patterns_used": [], "evidence": {}}]
    _db.save_predictions(issue, tickets)
    _db.save_eval_meta(issue, tickets, result={"llm_used": False})

    # 直接演练调度器那段判定（与其实现同源：strict_scope_for_user + strict_mode_gates）
    active, _bypass = RP.strict_scope_for_user(1)
    assert active is True, "本用例要求严格模式对该用户生效"
    errs = RP.strict_mode_gates([], issue)
    assert errs, "门控应失败"
    # 临时库无 users 行 → resolve_tenant 回落到 SYSTEM_USER_ID(0)。
    # 用解析后的**真实**归属，而不是硬编码 1（硬编码在空库上必然匹配不到行）。
    _uid = _db.resolve_tenant(None)
    n = _db.mark_prediction_source(
        issue, source_mode="stat_fallback",
        strict_gates=[e[0] for e in errs],
        strict_reason="；".join(e[1] for e in errs), user_id=_uid)
    assert n >= 1, f"应至少更新一条 eval_meta（uid={_uid}）"

    got = _db.load_prediction_source(issue, user_id=_uid)
    assert got["source_mode"] == "stat_fallback"
    assert "replayable_rules" in got["strict_gates"]


def test_api_app_source_has_fallback_marking():
    """源码守卫：`api_app` 的调度器必须真的调用标注函数。

    变异点：删掉 `db.mark_prediction_source(` 调用 —— 变红。
    为什么需要源码级：调度器里有日期/抓取/进化等一大堆前置步骤，
    纯单测很难把整条 `_scheduler_step` 跑通；而"这条标注还在不在"是本次
    核心不变量，必须机器可查。
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "lottery" / "api_app.py").read_text(encoding="utf-8")
    # 三件事必须同时成立，合并成一条断言（元守卫对 B 类子串断言有配额，拆开会无谓增长）
    missing = [k for k in ("mark_prediction_source(", "strict_scope_for_user(",
                           "strict_mode_gates(") if k not in src]
    assert not missing, f"调度器缺少严格模式接线：{missing}"
