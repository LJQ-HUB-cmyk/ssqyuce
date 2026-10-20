"""FastAPI 应用：REST API + 静态单页前端。"""
from __future__ import annotations

import json
import contextvars
import hmac
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

class _NoCacheStaticFiles(StaticFiles):
    """静态资源禁用缓存，确保前端更新后浏览器立即拿到新版。"""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

from . import config, data_fetcher, db
from . import auth_api
from . import user_llm_api
from . import routes_llm
from . import routes_tasks
from . import routes_eval
from . import routes_llm_eval
from . import routes_methods
from . import routes_ml
from . import routes_rigor
from . import routes_portfolio
from . import routes_data
from . import routes_patterns
from . import routes_predict
from . import routes_diagnosis
from . import routes_llm_ops
# P2：`_jsonable` 已被 10 个端点共用，提到 jsonutil 以免各域反向依赖 api_app。
# 保留同名别名，既有调用点与测试零改动。
from .jsonutil import jsonable as _jsonable
from .decision_skill import DECISION_SKILL_BASELINE  # noqa: E402  常量已随服务模块搬走
# P2：这些符号已随域切分搬走，但**既有测试与内部调用按名字 import 它们**。
# 名字是契约、实现位置不是 —— 这里显式再导出，避免破坏外部引用。
# 用 `# noqa: F401` 标明是有意保留的未使用导入。
from .decision_skill import (  # noqa: F401
    _passed_pattern_count,
    _usable_decision_count,
    _wilson_lower,
    decide_from_wilson,
    decision_skill,
    llm_marginal_value,
)
from .routes_portfolio import (  # noqa: F401
    _hypergeom,
    ticket_expected_prize,
)
TICKET_PRICE = config.TICKET_PRICE   # noqa: F401  已提到 config（领域常量）
# 数据域端点同样按名字被测试引用（`from lottery.api_app import app_info`），
# 再导出以保持契约。
from .routes_data import app_info, health  # noqa: F401
# 其余被测试按名字引用的内部符号（严格模式门控 / 决策自动关停）。
# 这两个是历史缺陷最集中的地方，测试直接打它们 —— 保留名字就是保留可测性。
from .routes_predict import (  # noqa: F401
    _strict_mode_reject_response,
    _strict_scope_for_current_user,
)
from .routes_llm_ops import _maybe_auto_disable_decision  # noqa: F401
from .bg_tasks import _bg_run  # noqa: F401  已提为共享基础设施（多域共用）
from . import migrations
from . import features as F, diagnose as D, mining as M

# 当前请求的用户上下文（仅 contextvar；**不用于 api_app→db 的租户传参**，
# 那条路径必须用显式参数 —— 裸 threading.Thread 不继承 contextvar，
# 丢失后会静默落到 SYSTEM 写错数据，而且测试不会红。见 Lead 裁决 9。）
#
# P2：contextvar 本身已搬到 `lottery/request_context.py`。原因是依赖方向倒置——
# `db.py` 和 `llm_client.py` 是**底层库**，却为了读一个 contextvar 反向依赖
# 3500 行的 Web 应用模块（靠把 import 写进函数体来躲循环）。搬到独立模块后
# 变成 `api_app → request_context ← db`，方向单向。
#
# 这里保留同名别名，是为了不破坏既有调用点与测试里的
# `monkeypatch.setattr(api_app, "_current_user_id", ...)`。
from .request_context import (                    # noqa: E402
    _current_user_id,
    current_user_id,
    reset_current_user_id,
    set_current_user_id,
)

app = FastAPI(
    title="双色球智能预测分析系统",
    version=config.APP_VERSION,
    # W01：文档端点默认关闭。`/docs` `/redoc` `/openapi.json` **不在 `/api/` 前缀下**，
    # 天然绕过 `optional_api_auth`，是当前生产最容易被忽略的暴露面（实测公网 200）。
    # 本地开发需要时用 `LOTT_API_DOCS=1` 打开。
    docs_url="/docs" if config.API_DOCS_ENABLED else None,
    redoc_url="/redoc" if config.API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if config.API_DOCS_ENABLED else None,
)
# W01：CORS 收敛到显式白名单（原先 `["*"]`）。前端 `app.js` 用相对路径 fetch，
# 属同源请求、本不依赖 CORS；放开 `*` 只会扩大第三方页面的跨域读取面。
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── MU-1 / W05：身份解析的**唯一入口**（Lead 裁决 8）──────────────────────
#
# 为什么必须是模块级函数，而不是中间件里的内联逻辑：
#   `app.dependency_overrides` 对 HTTP 中间件**无效**。若解析内联在中间件里，
#   测试就无法用一个 fixture 统一注入身份，只能把 36 处 `TestClient` 逐个改成
#   "注入真实 session cookie" —— 爆炸半径从 1 处变成 36 处。
#   外提之后 conftest 一行 monkeypatch 即全局生效：
#       monkeypatch.setattr(api_app, "_resolve_session_user_id", lambda r: ADMIN_ID)
#
# 三条纪律：① 模块级；② 同步签名（不起协程）；③ 不读 contextvar、不依赖中间件状态。
# **无有效会话时返回 None，绝不回退 admin / SYSTEM_USER_ID**（那是提权后门）。
def _resolve_session_user_id(request: Request):
    """Cookie → user_id；无效/未登录返回 ``None``。"""
    from . import auth_api
    sess = auth_api.session_from_request(request)
    if not sess:
        return None
    return int(sess["user"]["user_id"])


def _load_active_user_for_middleware(user_id: int):
    from . import auth
    return auth.load_active_user(int(user_id))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """多用户鉴权（deny-by-default）+ 旧 LOTT_TOKEN 兼容。

    执行顺序（U1 §7.3，**顺序不可调换**）：
      1. 求所需角色（公开 → 直接放行）
      2. 解析身份（唯一入口 `_resolve_session_user_id`）
      3. **status 闸门**：只接受 `status='active'`（bootstrap/locked/disabled 一律 403）
      4. **角色闸门**：admin-only 路径校验角色
      5. 注入 contextvar（供 llm_client 等深层库读取），finally 必须清理

    第 3 步必须先于第 4 步：`status='bootstrap'` 的行可能写着 `role='admin'`，
    若先判角色再判 status，这一行就是一个**零密码管理员**。

    `config.AUTH_MODE != 'enforce'` 时只做"解析 + 记录"，不拦截 —— 这是留给
    "前端登录页尚未落地"的灰度期。切换前必须完成前端登录页与引导建号。
    """
    from . import auth_api

    path = request.url.path
    need = auth_api.required_role(path)

    # ① 旧 LOTT_TOKEN 兼容层（**先于**多用户分级，恢复既有语义）：
    #    配了 LOTT_TOKEN 时保护所有 /api/*，唯二例外是 /api/health（容器探活）
    #    与登录/引导入口（否则本身就要 token 才能登录，死锁）。
    #    这条曾经被写成"只对公开路径跳过校验"，导致配了 token 后 /api/info
    #    反而公开 —— 既有测试 test_m4_5_engineering 抓住了它。教训：
    #    **白名单只能收窄，不能因为"路径小"就顺手放行**。
    if config.API_TOKEN and path.startswith("/api/") \
            and path not in _TOKEN_EXEMPT:
        if not _token_ok(request):
            return _unauthorized_bearer()

    if need is None:
        return await call_next(request)

    # ② 解析身份
    uid = None
    try:
        uid = _resolve_session_user_id(request)
    except Exception:  # noqa: BLE001 —— 解析失败一律视为未登录，绝不放行
        uid = None

    # 灰度期：不拦截，但把身份写进 contextvar，便于后续按用户化改造渐进生效
    if not config.auth_enforced():
        if uid is not None:
            _current_user_id.set(uid)
        try:
            return await call_next(request)
        finally:
            _current_user_id.set(None)

    if uid is None:
        # 旧 LOTT_TOKEN 仍可作为"机器对机器"的兼容通道（若部署时配了）
        if config.API_TOKEN and _token_ok(request):
            return await call_next(request)
        return JSONResponse({"ok": False, "error": "未登录",
                             "auth_required": True}, status_code=401,
                            headers={"WWW-Authenticate": "Cookie"})

    # ③ status 闸门（先于角色判断）
    user = _load_active_user_for_middleware(uid)
    if user is None:
        auth_api.auth.audit("authz.denied", actor_id=uid, ip=auth_api.client_ip(request),
                            result="denied",
                            detail={"path": path, "reason": "inactive_status"})
        return JSONResponse({"ok": False, "error": "账户不可用"}, status_code=403)

    # ④ 角色闸门
    if need == "admin" and user.get("role") != "admin":
        auth_api.auth.audit("authz.denied", actor_id=uid, ip=auth_api.client_ip(request),
                            result="denied",
                            detail={"path": path, "required": "admin"})
        return JSONResponse({"ok": False, "error": "需要管理员权限"}, status_code=403)

    # ⑤ contextvar：仅供 llm_client 这类**无法改签名**的深层库读取
    _current_user_id.set(uid)
    try:
        return await call_next(request)
    finally:
        _current_user_id.set(None)      # AnyIO 工作线程会被复用，不清理会串身份


# 旧 LOTT_TOKEN 的豁免路径（**必须保持极小**）：只有容器探活与登录入口。
# 其余 /api/* 在配置了 LOTT_TOKEN 时一律要求 Bearer —— 这是 V4 起就有的语义，
# 多用户改造不得放宽它（放宽等于给存量部署留了一个静默后门）。
_TOKEN_EXEMPT = frozenset({"/api/health", "/api/auth/login", "/api/auth/bootstrap",
                           "/api/auth/logout", "/api/auth/me"})


def _token_ok(request: Request) -> bool:
    """旧 LOTT_TOKEN 校验（Bearer）。"""
    if not config.API_TOKEN:
        return False
    got = request.headers.get("authorization", "")
    return hmac.compare_digest(got, f"Bearer {config.API_TOKEN}")


def _unauthorized_bearer() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "需要 Bearer Token"}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer"})


# 认证与管理路由（登录/登出/me/引导 + 用户管理 + 审计）
app.include_router(auth_api.router)
# W08a：按用户隔离的 LLM 配置（当前归属规则见 user_llm_api 模块 docstring）
app.include_router(user_llm_api.router)
# P2：按域切分的路由模块。api_app 只负责组装（建 app / 中间件 / include / 静态）。
app.include_router(routes_llm.router)
app.include_router(routes_tasks.router)
app.include_router(routes_eval.router)
app.include_router(routes_llm_eval.router)
app.include_router(routes_methods.router)
app.include_router(routes_ml.router)
app.include_router(routes_rigor.router)
app.include_router(routes_portfolio.router)
app.include_router(routes_data.router)
app.include_router(routes_patterns.router)
app.include_router(routes_predict.router)
app.include_router(routes_diagnosis.router)
app.include_router(routes_llm_ops.router)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- 页面 ----------



@app.get("/")
def index():
    resp = FileResponse(WEB_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------- 开奖日自动调度（可选，LOTT_SCHEDULER=1） ----------

def _scheduler_step():
    """开奖日（二/四/日）21:35 后：抓取 → 在线对照 → **跑一轮进化** → 生成下期预测。

    V5 起在生成预测之前先跑进化（``_evolution_step``），这样「找规律 → 实战验证 →
    用已验证规律预测」是同一个调度周期内完成的，而不是靠人工手动触发。
    """
    from . import backtest as BT, engine, evaluate
    now = __import__("datetime").datetime.now()
    if now.weekday() not in config.DRAW_WEEKDAYS:
        return
    if now.strftime("%H:%M") < config.DRAW_TIME:
        return
    try:
        info = data_fetcher.fetch_and_update()
        print("[scheduler] 数据已更新:", info.get("inserted_new"), "期")
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 抓取失败:", e)
        return
    check_result = evaluate.online_check()
    from . import notify
    print("[scheduler] 通知结果:", notify.notify_after_check(check_result))

    # V5：每期开奖后自动跑一轮进化（失败不影响预测生成）
    try:
        _evolution_step()
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 进化失败（不影响预测）:", e)

    # ── MU-0 / W06：前置注册的**写入侧**（这是全系统最上游的一处断链）──
    #
    # 背景（V13 + MU 评审实测）：`hypothesis.register_prereg()` 此前在非测试代码里
    # **调用点为 0**，于是：
    #   register_prereg 没人调用 → 假说没有 prereg_status
    #   → evaluate_prereg 只处理 prereg_status ∈ {registered,running,provisional}
    #   → prereg_ok 永远不可能为 True → usable_rules() 恒空
    #   → 界面永远「可用规律 0 条」，且**注入多强的真实效应都是 0**。
    #
    # **顺序必须在 evaluate_prereg 之前**：本期扫描出的候选应当在同一轮调度里
    # 就进入注册簿（虽然它要等 150 期冻结期后才判定），否则每期都"晚一期"。
    # 失败只降级、绝不中断调度（与既有约定一致）。
    try:
        from . import prereg_scheduler
        _scan = prereg_scheduler.register_scan_candidates(
            db.load_draws(), issue=str(draws[-1]["issue"]) if draws else "")
        print("[scheduler] 前置注册·写入:", {
            k: _scan.get(k) for k in
            ("ok", "scanned", "registered", "deduped", "judgeable", "error")})
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 前置注册·写入失败（不影响预测）:", e)

    # V10-02：前置注册判定（**唯一写者**的调用点，见 docs/V10_IMPROVEMENT_PLAN.md §3.3.5b）
    # 必须在快照刷新**之前**跑，否则本期已到期的实验要等下一期才生效（时序错误）。
    # 失败只降级、不中断（沿用 _evolution_step 的既有约定：判定失败只影响 usable 条数）。
    try:
        from . import hypothesis
        # issue=None → 由 evaluate_prereg 从库里读最近一期作为"当前期号"
        _prereg = hypothesis.evaluate_prereg()
        print("[scheduler] 前置注册判定:", {k: _prereg.get(k) for k in
                                            ("checked", "passed", "provisional", "failed",
                                             "invalidated", "n_tested_cumulative")})
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 前置注册判定失败（不影响预测）:", e)

    # V7：生成预测前先重建规律快照，让「刚验证出来的规律」本期就能进入出号链路。
    # 失败只记录、不影响预测（快照是可选加速层，缺失时引擎照常出号）。
    try:
        from . import patterns_runtime
        print("[scheduler] 规律快照:", patterns_runtime.refresh(db.load_draws()))
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 规律快照刷新失败（不影响预测）:", e)

    # V11：每期自动推进规律生命周期（退居二线判定）。
    # 顺序：在快照刷新**之后**、生成预测**之前** —— 这样本期被判定为
    # 退居二线（monitor）的规律，会通过下一轮 build_snapshot 注入的
    # lifecycle 状态立即从 usable_rules() 里消失，本期出号就不再使用它。
    # 失败只降级（沿用既定约定），绝不影响当期预测生成。
    try:
        from . import rule_lifecycle as _RL
        _cycle = _RL.run_cycle(db.load_draws(), use_llm=False, persist=True)
        _mig = _cycle.get("migrated") or []
        print("[scheduler] 生命周期:", "迁移", len(_mig),
              "条" if _mig else "无需迁移", "|",
              {k: v for k, v in (_cycle.get("counts") or {}).items()
               if k in ("active", "monitor", "unmanaged")})
    except Exception as e:  # noqa: BLE001
        print("[scheduler] 生命周期推进失败（不影响预测）:", e)

    draws = db.load_draws()
    exp = BT.expected_issue(draws)
    if exp.get("expected_draws_missing"):
        print("[scheduler] 警告：数据源落后于开奖日历，缺", exp.get("missed_draws"), "期")
    issue = exp["issue"]
    if not db.load_predictions(issue):
        # V10-11：决策通道被关停时不再请求 LLM（省钱且不产出被关停通道的注），
        # 但预测**照常生成**（这就是与 LLM_DISABLED 的区别：后者会 503）。
        from . import methods as METH
        _llm_on = not bool(METH.resolve_channels().get("decision_disabled"))
        engine.predict_next(draws, use_llm=_llm_on)
        print("[scheduler] 已生成", issue, "预测",
              "（LLM 决策通道已关停，本次为纯统计/ML）" if not _llm_on else "")


def _evolution_step(limit: int = 0, use_llm: bool = True,
                    force: bool = False) -> dict:
    """每期开奖后自动跑的一轮自进化：挖掘 → 注册 → A/B 实战验证 → 裁决。

    为什么需要它：``evolve_once`` 只做「挖掘+注册+应用」，假说会永远停在
    ``proposed``（实测 9 条全是 proposed），因为**没有任何环节把它们送去做 A/B 验证**。
    本函数补上这一步——对每条 proposed 假说跑配对回放，把结果交给
    ``hypothesis.resolve_registry`` 裁决，假说才可能晋升 ``validated``（进而影响出号）
    或被 ``rejected``（退场）。

    成本控制：
    - ``limit`` 限制参与验证的历史期数（0=全部）；A/B 内部另有 600 期上限
    - ``use_llm`` 控制是否调用 LLM 提炼新假说（单次 1~2 分钟）
    - **幂等（2026-10-19 CPU 治理）**：同一期数据只自动进化一次。见下方说明。
    - 整体失败只记录不抛出，绝不影响当期预测生成

    ⚠️ 为什么要幂等（实测根因）
    -------------------------
    ``_scheduler_step`` 由 ``_scheduler_loop`` 每 1800s 调用一次；在**开奖日**
    （周二/四/日）过了 21:35 之后，每轮都会走到这里。而这一天剩下的时间里数据
    并不会再变（``inserted_new: 0``），于是**同一份数据被反复跑完整进化**：
    日志实测 2026-10-18（周日）14:13 / 15:06 / 15:56 三轮，每轮 19~22 分钟。
    这就是「容器 CPU 长期 100%」的第二层成因（第一层是 ``_rebuild_probs`` 的
    冗余模型构建，已单独修复）。

    修法：把「已进化到的数据指纹（期数 + 末期号）」记在 ``data/evolution_state.json``；
    指纹没变就**跳过**（返回 ``skipped=True``）。数据一更新指纹就变 ⇒ 立刻恢复进化。
    手动触发（``/api/evolution/run``）传 ``force=True``，永不被跳过。

    返回本轮摘要，便于调度日志与 ``/api/evolution/status`` 观测。
    """
    from . import battle, evolution, hypothesis

    draws = db.load_draws()
    if not draws:
        return {"ok": False, "reason": "本地暂无开奖数据"}

    # ---- 幂等闸门：同一份数据只自动进化一次 ----
    fingerprint = f"{len(draws)}:{draws[-1].get('issue')}"
    if not force:
        state = _evolution_state_load()
        if state.get("last_fingerprint") == fingerprint:
            return {"ok": True, "skipped": True,
                    "skipped_reason": "该期数据已完成进化（幂等跳过）",
                    "fingerprint": fingerprint,
                    "last_run_at": state.get("last_run_at"),
                    "note_zh": ("本期数据未变化，进化已跑过 → 跳过（避免每 30 分钟"
                                "重复跑完整 A/B 验证；手动触发请用 /api/evolution/run）")}

    if limit:
        draws = draws[-int(limit):]

    summary: dict = {"ok": True}

    # 1) 挖掘 + 注册新假说（LLM 或纯统计）
    mined = hypothesis.mine_hypotheses({}, draws, use_llm=use_llm)
    reg = hypothesis.register_hypotheses(mined.get("hypotheses") or [])
    summary["mined"] = {"stat": mined.get("stat_count"), "llm": mined.get("llm_count"),
                        "llm_used": mined.get("llm_used"), "llm_error": mined.get("llm_error")}
    summary["registered"] = reg

    # 1b) V6：把「规律工坊」里已验证过阶段的 LLM 规律也注册进假说库
    #     （只收 support/reject/regime_only，unmappable/noise 一律不入库）
    try:
        from . import llm_lab
        if llm_lab.load_claims().get("claims"):
            summary["llm_lab_registered"] = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        summary["llm_lab_registered"] = {"error": f"{type(e).__name__}: {e}"}

    # 2) 对**全部** proposed 假说做实战验证 —— 这是它们唯一的晋升通道。
    #    V6：按假说类型分派验证口径。维度化的走 as-of 筛选条件回放（任何假说都有
    #    非平凡数字），其余走结构/方法权重的 A/B 配对回放。
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    proposed = [h for h in hyps if h.get("status") == "proposed"]
    results = []
    for h in proposed:
        act = h.get("action") if isinstance(h.get("action"), dict) else {}
        try:
            if act.get("type") in ("prefer_red_filter", "prefer_blue_filter"):
                results.append(evolution.verify_discovery_hypothesis(h, draws, start=200))
            else:
                results.append(evolution.verify_hypothesis_ab(
                    h, draws, n_tickets=5, start=max(300, len(draws) - 600)))
        except Exception as e:  # noqa: BLE001
            print(f"[evolution] 假说 {h.get('id')} 验证失败: {e}")
    summary["verified"] = len(results)

    # 3) 裁决：validated / rejected / 样本不足
    resolution = hypothesis.resolve_registry(results) if results else {}
    summary["resolution"] = {
        "validated": resolution.get("validated"),
        "rejected": resolution.get("rejected"),
        "insufficient": len(resolution.get("insufficient") or []),
        "unchanged": resolution.get("unchanged"),
    }

    # 3b) V6：阶段时效标注（每条假说带「现在是否还有效」）
    try:
        summary["regime_annotated"] = evolution.annotate_hypotheses(draws)
    except Exception as e:  # noqa: BLE001
        summary["regime_annotated"] = {"error": f"{type(e).__name__}: {e}"}

    # 4) 报告当前生效的出号策略覆盖项
    st = evolution.evolution_status()
    summary["by_status"] = st.get("by_status")
    summary["active_overrides"] = st.get("active_overrides")

    # 5) V10-11：决策技能达标性判定（Wilson 下界 ≤ 0.5 → 自动关停决策通道）。
    #    写 data/llm_config.json（不写 os.environ）；无决定性样本时**不**写。
    #    失败只降级、不影响预测（沿用本函数的既有约定）。
    try:
        summary["decision_skill"] = _maybe_auto_disable_decision()
    except Exception as e:  # noqa: BLE001
        summary["decision_skill"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print("[evolution] 本轮完成:", summary["resolution"], "| 库:", summary["by_status"])
    # 记录本次进化对应的数据指纹（幂等闸门的数据源）。写失败只记录、不影响结果。
    try:
        _evolution_state_store(fingerprint, summary)
    except Exception as e:  # noqa: BLE001
        print("[evolution] 状态记录失败（不影响进化结果）:", e)
    summary["fingerprint"] = fingerprint
    return summary


def _evolution_state_path():
    """进化幂等状态文件路径（与 ``adaptive_state.json`` 同目录，属运行时状态）。"""
    return config.DATA_DIR / "evolution_state.json"


def _evolution_state_load() -> dict:
    """读幂等状态；文件缺失/损坏一律返回空 dict（当作「从未进化」）。"""
    import json as _json
    try:
        p = _evolution_state_path()
        if not p.exists():
            return {}
        raw = _json.loads(p.read_text(encoding="utf-8") or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _evolution_state_store(fingerprint: str, summary: dict) -> None:
    """记录「已进化到的数据指纹」（原子替换）。"""
    import json as _json
    import os as _os
    import time as _time
    p = _evolution_state_path()
    payload = {
        "last_fingerprint": str(fingerprint),
        "last_run_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_resolution": summary.get("resolution"),
        "schema": 1,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _os.replace(tmp, p)


def _scheduler_loop():
    import time as _time
    while True:
        try:
            _scheduler_step()
        except Exception as e:  # noqa: BLE001
            print("[scheduler] 异常:", e)
        _time.sleep(1800)


# ---------- V5：战斗回放 + 自进化（历史规律 → 实战逐期验证 → 自我改进） ----------
#
# 用户口径：准确性 = 基于历史数据找规律 → 用于实战 → 用实战结果对比。
# 这三个端点把该口径落地为可查询的实战成绩单，而不是理论推演：
#   · /api/battle/report   全方法在 N 期真实开奖上的逐期战绩 + 两两显著性
#   · /api/battle/compare  只要对比结论（前端卡片用）
#   · /api/evolution/status 假说库与进化进度
#
# battle / hypothesis / evolution 三模块并行开发，任一未就绪都必须降级而非 500。

def _battle_or_503():
    """导入 battle 模块；不可用时返回 (None, 503 响应)。"""
    try:
        from . import battle
    except ImportError as e:
        return None, JSONResponse(
            {"ok": False, "available": False,
             "error": "战斗回放模块 lottery.battle 尚未就绪",
             "detail": str(e), "hint": "该端点需 lottery/battle.py；其余功能不受影响。"},
            status_code=503)
    return battle, None


# ---------- V6：阶段有效性（regime）+ 多维度规律发现 + LLM 规律工坊 ----------

@app.get("/api/regime/overview")
def regime_overview(limit: int = Query(0, ge=0, le=3490),
                    era_mode: Optional[str] = Query(None),
                    era_block: int = Query(0, ge=0, le=600)):
    """阶段时效总览：各阶段边界 + 全部规律的阶段画像 + 状态机分布。

    回答用户最关心的两个问题：**规律在哪段时间有效**、**现在还灵不灵**。
    """
    try:
        from . import evolution, hypothesis, regime
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "阶段时效模块尚未就绪", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    if limit:
        draws = draws[-int(limit):]
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    hyps = evolution._as_hyp_list(hypothesis.load_registry())
    items = []
    for h in hyps:
        rep = h.get("regime") or {}
        items.append({
            "id": h.get("id"), "family": h.get("family"),
            "statement": h.get("statement"), "status": h.get("status"),
            "action_type": (h.get("action") or {}).get("type"),
            "regime_status": rep.get("status"),
            "regime_label": rep.get("label_zh"),
            "regime_reason": rep.get("reason_zh"),
            "recent_effect": rep.get("effect"),
            "p_adjusted_recent": rep.get("p_adjusted_recent"),
            "n_sig_eras": rep.get("n_sig_eras"),
            "last_significant_era": rep.get("last_significant_era"),
        })
    counts: Dict[str, int] = {}
    for it in items:
        k = it.get("regime_status") or "unannotated"
        counts[k] = counts.get(k, 0) + 1
    return {
        "ok": True,
        "eras": regime.eras_for(draws, mode=(era_mode or regime.ERA_MODE),
                                block=(era_block or None)),
        "era_mode": (era_mode or regime.ERA_MODE),
        "era_block": regime.ERA_BLOCK if (era_mode or regime.ERA_MODE) == "adaptive" else None,
        "era_modes": {
            "adaptive": {"label": "自适应（按固定期数）",
                         "block": regime.ERA_BLOCK,
                         "note": "按 150 期一段切分：单段样本更大、检测力更高；近年与早年同等对待"},
            "calendar": {"label": "按自然年（9 段）",
                         "note": "2003-2008/…/2026；近年单段仅 118~151 期，检测力低，仅供对照"},
        },
        "era_defs": [{"name": n, "start_year": lo, "end_year": hi}
                     for n, lo, hi in regime.ERA_DEFS],
        "recent_window": regime.RECENT_WINDOW,
        "stale_after_draws": regime.STALE_AFTER_DRAWS,
        "min_era_n": regime.MIN_ERA_N,
        "status_counts": counts,
        "status_legend": {k: regime_status_label(k) for k in
                          list(counts.keys()) + list(REGIME_STATUS_ORDER)},
        "items": items,
        "n_draws": len(draws),
        "note_zh": ("状态机口径：每个规律被切成 9 个阶段（近年更细），每阶段做显著性与"
                    "BH 校正；『陈旧』= 最近一次显著距今超过 "
                    f"{regime.STALE_AFTER_DRAWS} 期，正是「规律不再生效」的量化形态。"),
    }


REGIME_STATUS_ORDER = ["persistent", "emerging", "fading", "stale", "flipped",
                       "reversed", "noise", "no_dimension", "insufficient", "error"]


def regime_status_label(status: str) -> str:
    return {
        "persistent": "持续有效（最近阶段仍显著）",
        "emerging": "新兴线索（只有最近阶段显著，未确认）",
        "fading": "正在衰减（上一阶段有效，最近失效）",
        "stale": "陈旧（曾显著，已被新数据推翻）",
        "flipped": "方向反转（疑似伪规律）",
        "reversed": "已失效（仅早期阶段显著）",
        "noise": "未检出（各阶段均不显著）",
        "no_dimension": "无可回放维度（结构类/权重类）",
        "insufficient": "样本不足",
        "error": "阶段分析失败",
        "unannotated": "尚未标注（点「重算阶段时效」）",
    }.get(status or "", status or "")


@app.post("/api/regime/refresh")
def regime_refresh(start: int = Query(200, ge=60, le=3000)):
    """重算全部假说的阶段时效标注（写回 data/hypotheses.json）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "阶段时效模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = evolution.annotate_hypotheses(draws, start=int(start))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "标注失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@app.get("/api/discovery/families")
def discovery_families():
    """维度表：可搜索的规律空间（20+ 族、90+ 组参数）。"""
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    fams = discovery.family_catalog()
    dims: Dict[str, int] = {}
    for f in fams:
        dims[f["dim_zh"]] = dims.get(f["dim_zh"], 0) + f["n_param_sets"]
    return {"ok": True, "families": fams, "n_families": len(fams),
            "n_combos": discovery.combo_space_size(), "dims": dims}


@app.get("/api/discovery/scan")
def discovery_scan(top: int = Query(12, ge=1, le=60),
                   fast_window: int = Query(600, ge=200, le=3000),
                   families: Optional[str] = Query(None),
                   refresh: int = Query(0, ge=0, le=1)):
    """多维度 as-of 扫描：枚举维度×参数，给每条候选做阶段画像。

    严格 as-of：第 t 期只用 draws[:t] 计算筛选条件；扫描的搜索成本（已测组合数、
    纯随机预期假阳性数）一并返回，避免把「搜出来的」当「发现的」。
    """
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    flist = [x.strip() for x in families.split(",") if x.strip()] if families else None
    try:
        res = discovery.discover(draws, top=int(top), fast_window=int(fast_window),
                                 families=flist, use_cache=not bool(refresh))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "扫描失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    out = dict(res)
    out["ok"] = True
    out["n_draws"] = len(draws)
    return out


def _llm_lab_modules():
    from . import discovery, llm_lab
    return discovery, llm_lab


@app.get("/api/llm_lab/latest")
def llm_lab_latest(limit: int = Query(40, ge=1, le=200)):
    """最近一次规律工坊的结果（LLM 规律 + 逐条落地验证 + 阶段画像）。"""
    try:
        _, llm_lab = _llm_lab_modules()
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "LLM 规律工坊模块尚未就绪", "detail": str(e)},
                            status_code=503)
    data = llm_lab.load_claims()
    claims = (data.get("claims") or [])[:int(limit)]
    counts: Dict[str, int] = {}
    for c in claims:
        k = c.get("verdict") or "unknown"
        counts[k] = counts.get(k, 0) + 1
    return {"ok": True, "available": True, "updated_at": data.get("updated_at"),
            "runs": (data.get("runs") or [])[:5],
            "claims": claims, "verdict_counts": counts,
            "verdict_legend": llm_lab.VERDICTS,
            "n_stored": len(data.get("claims") or [])}
@app.post("/api/llm_lab/register")
def llm_lab_register():
    """把工坊里 verified 的规律注册进假说库（unmappable/noise 不入库）。"""
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "进化模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = evolution.register_llm_lab_claims(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "注册失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}


@app.get("/api/backtest/regime")
def backtest_regime(key: str = Query(..., min_length=1),
                    params: Optional[str] = Query(None),
                    start: int = Query(200, ge=60, le=3000)):
    """任意维度组合的**检索式核验**：as-of 回放 + 阶段时效（前端「核验」按钮用）。

    ``params`` 是 JSON 字符串，例如 ``{"window":30,"k":6}``。
    """
    try:
        from . import discovery
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "维度模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        p = json.loads(params) if params else (discovery.FAMILY_BY_KEY.get(key) or {}).get("params", [{}])[0]
    except (json.JSONDecodeError, TypeError):
        return JSONResponse({"ok": False, "error": f"params 不是合法 JSON：{params}"},
                            status_code=400)
    try:
        flt = discovery.make_filter(key, p or {})
        rep = discovery.regime_of_filter(draws, flt, start=int(start))
    except KeyError as e:
        return JSONResponse({"ok": False, "error": f"未知维度：{e}"}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "回放失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, "filter": {"key": key, "params": p, "label": flt["label"],
                                   "target": flt["target"]}, "regime": rep}


@app.get("/api/rules/lifecycle")
def rules_lifecycle_status():
    """V11：规律生命周期状态（谁在一线 / 谁退居二线 / 谁被滞后暂缓）。

    来源唯一：``rule_lifecycle.load_states()``。返回体里
    ``demoted`` 是已退居二线（monitor）的规律 id 及其理由。
    """
    from . import rule_lifecycle as RL

    states = RL.load_states()
    recs = (states or {}).get("rules") or {}
    demoted = {rid: r for rid, r in recs.items()
               if (r or {}).get("state") == "monitor"}
    active = {rid: r for rid, r in recs.items()
              if (r or {}).get("state") == "active"}
    return {
        "ok": True,
        "thresholds": (states or {}).get("thresholds") or RL.thresholds(),
        "version": (states or {}).get("version"),
        "n_managed": len(recs),
        "n_active": len(active),
        "n_demoted": len(demoted),
        "demoted": demoted,
        "active": active,
        "state_file": str(RL.STATE_FILE),
        "note_zh": ("退居二线（monitor）= 衰减判据连续成立，不再参与出号，"
                    "但仍留在列表里继续观察 —— 不是被证伪。"),
    }


@app.post("/api/rules/lifecycle/run")
def rules_lifecycle_run(use_llm: bool = Query(False)):
    """V11：手动推进一次规律生命周期。

    ``use_llm=False``（默认）：纯确定性（不调 LLM）；LLM 的复核意见
    永远不会影响状态迁移（纪律 1），所以手动触发默认不依赖它。

    返回统一走 ``rule_lifecycle.run_cycle`` 的结构化结果（含 migrated /
    counts / rules / note_zh）。
    """
    from . import patterns_runtime, rule_lifecycle as RL

    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    snap = patterns_runtime.build_snapshot(draws)
    try:
        out = RL.run_cycle(draws, snapshot=snap, use_llm=bool(use_llm), persist=True)
    except Exception as exc:                              # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"生命周期推进失败: {exc}"},
                            status_code=200)
    out.setdefault("note_zh", _lifecycle_note(out))
    return _jsonable(out)


def _lifecycle_note(out: dict) -> str:
    changed = out.get("changed") or {}
    if not changed:
        return ("本期没有规律需要退居二线（全部 hold）——"
                "这不是功能失效，而是规律本身不存在衰减证据")
    items = [f"{rid}: {v.get('from')}→{v.get('to')}（{'、'.join(v.get('reasons') or [])}）"
             for rid, v in list(changed.items())[:10]]
    return f"本次迁移 {len(changed)} 条：" + "；".join(items)


@app.get("/api/rules/runtime")
def rules_runtime():
    """规律运行时快照：现在到底有哪些规律「可用」、哪些只监控、哪些已否决。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "规律运行时模块尚未就绪", "detail": str(e)},
                            status_code=503)
    draws = db.load_draws()
    current = draws[-1]["issue"] if draws else None
    snap = patterns_runtime.load_snapshot(current_issue=current)
    if not snap:
        return {"ok": True, "available": False, "stale": True,
                "note_zh": "尚未生成规律快照（点「刷新规律快照」或等调度器刷新）"}
    return {"ok": True, "available": True,
            "apply_enabled": patterns_runtime.apply_enabled(),
            "current_issue": current, **{k: snap.get(k) for k in
                                         ("generated_at_text", "base_issue", "n_draws",
                                          "counts", "scan", "eras", "stale", "age_text")},
            "rules": snap.get("rules") or []}


@app.post("/api/rules/refresh")
def rules_refresh():
    """重算规律运行时快照（阶段时效 + 维度扫描 + 假说库 → 快照文件，约 5~20 秒）。"""
    try:
        from . import patterns_runtime
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "规律运行时模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    try:
        r = patterns_runtime.refresh(draws)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "刷新失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return {"ok": True, **r}
@app.post("/api/rules/closed-loop/async")
def rules_closed_loop_async(rounds: int = Query(3, ge=1, le=3),
                            use_llm: int = Query(1, ge=0, le=1)):
    """**AI 规律闭环（后台任务版）**：立即返回 task_id，前端轮询进度。

    闭环内容与 ``/api/rules/closed-loop`` 相同（挖掘→验证→注册→刷新快照），
    但不再让浏览器干等：每完成一个阶段就更新任务的 progress/message。
    """
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    tid = _bg_run("closed_loop",
                  lambda p: llm_lab.run_closed_loop(draws, rounds=int(rounds),
                                                    use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "AI 规律闭环已在后台运行（约 1~8 分钟），可离开本页，完成后自动刷新"}


@app.post("/api/llm_lab/run/async")
def llm_lab_run_async(rounds: int = Query(3, ge=1, le=3),
                      use_llm: int = Query(1, ge=0, le=1)):
    """LLM 规律工坊（后台任务版），与 ``/api/llm_lab/run`` 同逻辑。"""
    try:
        from . import llm_lab
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "LLM 规律工坊模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "error": "本地暂无开奖数据"}, status_code=400)
    tid = _bg_run("llm_lab",
                  lambda p: llm_lab.run_lab(draws, rounds=int(rounds),
                                            use_llm=bool(use_llm), progress=p))
    return {"ok": True, "task_id": tid,
            "note_zh": "LLM 规律工坊已在后台运行（约 1~8 分钟），可离开本页"}


@app.get("/api/battle/report")
def battle_report(limit: int = Query(0, ge=0, le=3490),
                  n_tickets: int = Query(10, ge=1, le=20),
                  start: int = Query(300, ge=60, le=3000),
                  methods: Optional[str] = Query(None),
                  seed: int = Query(42)):
    """方法级 walk-forward 实战回放：各出号方法在真实开奖上的逐期战绩。

    limit=0 用全部历史（默认；计算量大，前端可按需传小值）。
    严格 walk-forward：评估第 t 期只用 draws[:t]，不泄漏未来信息。

    methods 逗号分隔可限定方法集（如 `uniform,stat:freq,blue_coverage`）以缩短耗时。
    """
    battle, resp = _battle_or_503()
    if resp is not None:
        return resp
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    if limit:
        draws = draws[-int(limit):]
    if len(draws) <= start:
        return JSONResponse({"ok": False, "available": True,
                             "error": f"样本不足：{len(draws)} 期 ≤ start={start}"},
                            status_code=400)
    mlist = [m.strip() for m in methods.split(",") if m.strip()] if methods else None
    try:
        report = battle.battle_report(draws, n_tickets=int(n_tickets),
                                      start=int(start), seed=int(seed))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "battle_report() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    if not isinstance(report, dict):
        return JSONResponse({"ok": False, "available": False,
                             "error": "battle_report() 返回结构异常"}, status_code=503)
    out = dict(report)
    out.setdefault("ok", True)
    out["available"] = True
    out["n_draws"] = len(draws)
    # 全量回放耗时较长；给出可复现的参数快照
    out["params"] = {"limit": limit, "n_tickets": n_tickets,
                     "start": start, "seed": seed, "methods": mlist}
    return out


@app.get("/api/evolution/status")
def evolution_status_endpoint():
    """自进化进度：假说库统计 + 当前生效的出号策略覆盖项。

    只展示 ``validated`` 的假说为「生效」——未经验证的假说不得影响真实出号。
    """
    try:
        from . import evolution
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "自进化模块 lottery.evolution 尚未就绪",
                             "detail": str(e)}, status_code=503)
    try:
        st = evolution.evolution_status()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "evolution_status() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    st = dict(st or {})
    st.setdefault("ok", True)
    return st


@app.post("/api/evolution/run")
def evolution_run(limit: int = Query(0, ge=0, le=3490),
                  n_tickets: int = Query(10, ge=1, le=20),
                  start: int = Query(300, ge=60, le=3000),
                  use_llm: int = Query(1, ge=0, le=1),
                  seed: int = Query(42)):
    """跑一轮**完整**进化：挖掘 → 注册 → A/B 实战验证 → 裁决 → 应用已验证规律。

    这是「自我完善、自我进化」的手动触发点，与调度器每期自动跑的
    ``_evolution_step`` 走同一条路径（避免两处逻辑漂移）。

    耗时较长：默认含 LLM 提炼（1~2 分钟）+ 对每条 proposed 假说做 600 期配对 A/B 验证。
    返回本轮摘要：挖到几条、注册几条、验证几条、裁决结果（validated/rejected/unchanged）、
    以及当前生效的出号策略覆盖项。
    """
    for mod in ("evolution", "hypothesis", "battle"):
        try:
            __import__(f"lottery.{mod}")
        except ImportError as e:
            return JSONResponse({"ok": False, "available": False,
                                 "error": f"自进化模块 lottery.{mod} 尚未就绪",
                                 "detail": str(e)}, status_code=503)
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    try:
        # 手动触发**必须**绕过幂等闸门（用户的意图就是「现在再跑一轮」）。
        result = _evolution_step(limit=int(limit), use_llm=bool(use_llm), force=True)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "_evolution_step() 执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    result = dict(result or {})
    result.setdefault("ok", True)
    # 附上当前进化状态，便于前端一次拿到完整画面
    try:
        from . import evolution as _evo
        result["status"] = _evo.evolution_status()
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------------------
# V12-13：AI 两岗位（核对员 / 契约检查器）+ 打分卡
#
# 铁律（V10 §3.2 两岗位共同约束）：这两条路由**不产号、不写 predictions、
# 不进候选池**；只读 ``llm_audit``（仅追加）或把判定扔进后台任务。
# 无记录时 ``available=False`` —— 前端必须显示"本次未取得 AI 核对结果"，
# **不许**渲染成"AI 已核对"。
# ---------------------------------------------------------------------------

@app.get("/api/llm/audit/latest")
def llm_audit_latest(post: Optional[str] = Query(None),
                     limit: int = Query(200, ge=1, le=500)):
    """最近一次 AI 核对判定 + 打分卡（只读）。

    ``post`` 可选 ``result_narrator`` / ``contract_checker``；不传则看全部。
    打分卡规则（V12 §1.3 硬性准入）：``n``（判定次数）、``score = n_correct /
    (n_correct+n_wrong+n_invalid)``，``skipped``（degraded）**单列、不进分母**。
    """
    try:
        from . import ai_audit
    except ImportError as e:
        return JSONResponse({"ok": False, "available": False,
                             "error": "AI 核对模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    if post is not None and post not in ai_audit.POSTS:
        return JSONResponse({"ok": False, "available": False,
                             "error": f"未知岗位：{post}（必须是 {ai_audit.POSTS}）"},
                            status_code=400)
    try:
        out = ai_audit.latest_audit(post=post)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "available": False,
                             "error": "读取审计记录失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    out = dict(out)
    out["ok"] = True
    out["posts"] = list(ai_audit.POSTS)
    return out


@app.post("/api/llm/audit/run")
async def llm_audit_run(request: Request,
                        post: str = Query(...),
                        limit_issues: int = Query(30, ge=1, le=200),
                        use_llm: int = Query(1, ge=0, le=1)):
    """跑一次 AI 核对（**后台任务**，V10 §3.2.3-2：预测请求绝不等 LLM）。

    * ``post=result_narrator``：输入来自真实 ``eval_details`` 最近 ``limit_issues`` 期
      （只读，不重新跑预测）；
    * ``post=contract_checker``：输入来自请求体 ``{"items": [{key, params, target}]}``
      （不传则取最近一次 ``llm_claims`` 快照中的 filters；都没有则 400）。

    立即返回 ``task_id``，前端轮询 ``/api/tasks/{id}``。单次只发 **1 次** LLM 调用。
    """
    try:
        from . import ai_audit
    except ImportError as e:
        return JSONResponse({"ok": False, "error": "AI 核对模块尚未就绪",
                             "detail": str(e)}, status_code=503)
    if post not in ai_audit.POSTS:
        return JSONResponse({"ok": False,
                             "error": f"未知岗位：{post}（必须是 {ai_audit.POSTS}）"},
                            status_code=400)
    body: Dict[str, Any] = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8") or "{}")
            if isinstance(parsed, dict):
                body = parsed
    except Exception:  # noqa: BLE001
        body = {}
    try:
        if post == ai_audit.POST_NARRATOR:
            rows = db.load_eval_details(limit_issues=int(limit_issues))
            if not rows:
                return JSONResponse({"ok": False,
                                     "error": "本地暂无 eval_details 记录（尚未有核对过的预测）"},
                                    status_code=400)
            audit_input = ai_audit.build_narrator_input(rows,
                                                        limit_issues=int(limit_issues))
        else:
            items = body.get("items")
            if items is None:
                items = _latest_claim_filters()
            if not items:
                return JSONResponse({"ok": False,
                                     "error": "没有可检查的 filters（请求体 items 为空且无 llm_claims 快照）"},
                                    status_code=400)
            audit_input = ai_audit.build_contract_input(items)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "构造核对输入失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)

    def _fn(_progress):
        _progress(0.1, "AI 核对运行中（单次调用）…")
        res = ai_audit.run_post(post, audit_input, use_llm=bool(use_llm),
                                persist=True)
        res["ok"] = True
        res["note_zh"] = res.get("note_zh") or "AI 核对完成"
        return res

    tid = _bg_run("llm_audit", _fn)
    return {"ok": True, "task_id": tid, "post": post,
            "note_zh": "已进入后台任务，前端轮询 /api/tasks/{id}"}


def _latest_claim_filters() -> List[Dict[str, Any]]:
    """从最近一次 ``llm_claims`` 快照里取出 filters（只读；没有则返回空列表）。"""
    try:
        from . import llm_lab
        claims = llm_lab.load_claims() or {}
    except Exception:  # noqa: BLE001
        return []
    items = claims.get("claims") or claims.get("items") or []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        flt = it.get("filter") or it.get("params") or {}
        out.append({"key": str(it.get("key") or flt.get("key") or ""),
                    "params": dict(it.get("params") or flt.get("params") or {}),
                    "target": str(it.get("target") or "red")})
    return [x for x in out if x["key"]]


# ==========================================================================
# V12-10 / V12-15：L2 结构层的两个 API（组合数学恒等式，不依赖历史数据）
#
# 共同纪律：
#   * 所有概率都是 C(33,6) **全枚举精确值**（``method="exact_enum"``），
#     与样本量 / p 值无关；
#   * 不改变、不宣称改变期望值（``ev_ratio_full`` 恒为单注口径）；
#   * **不给购买建议**（返回体里不得出现「划算」「推荐」等词，
#     ``tests/test_v12_l2_exact.py`` 用 grep 计数锁死）。
# ==========================================================================

@app.get("/api/l2/distribution")
def l2_distribution(n: int = Query(5, ge=1, le=16),
                    preset: str = Query("spread"),
                    seed: int = Query(0),
                    tickets: Optional[str] = Query(None)):
    """V12-10 L2 精确值服务：n 注票组的分布/尾部/覆盖精确值（中性读数）。

    入参：``n``（1..16）、``preset``（``spread`` 号位均摊 = 低重叠，``overlap``
    高重叠）、``seed``（造票种子，同 seed 同结果）、``tickets``（可选 JSON 字符串
    ``[{"reds":[..6..],"blue":1},...]``，给了就以它为准）。

    出参：``ticket_distribution()`` 原样字段 + ``method="exact_enum"``（断言非精确
    即 500）+ ``methods``（每个字段的方法标签）+ ``n``。
    """
    from . import l2_service
    try:
        payload = l2_service.l2_distribution_payload(
            n=int(n), preset=preset, seed=int(seed), tickets_json=tickets)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"参数错误: {exc}"},
                            status_code=400)
    except AssertionError as exc:          # exact_enum / prize_assumption 断言失败
        return JSONResponse({"ok": False, "error": f"L2 口径断言失败: {exc}",
                             "method": "unavailable"}, status_code=500)
    return _jsonable(payload)


@app.get("/api/portfolio/marginal")
def portfolio_marginal(n: int = Query(10, ge=1, le=15),
                       preset: str = Query("spread")):
    """V12-15 边际读数（L3-4）：「加到 n+1 注」的中性读数，**只给数字不给建议**。

    返回 ``delta_p_at_least_one_prize_pp``（+X 个百分点，全枚举精确值差分）、
    ``expected_return_yuan = −¥2×(1−ev_ratio_full)``（新增 1 注的期望回报）、
    ``ev_ratio_full``、``prize_assumption``、``n``、``n_next``、``method="exact_enum"``。
    """
    from . import l2_service
    try:
        payload = l2_service.marginal_reading(int(n), preset=preset)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"参数错误: {exc}"},
                            status_code=400)
    except AssertionError as exc:
        return JSONResponse({"ok": False, "error": f"L2 口径断言失败: {exc}",
                             "method": "unavailable"}, status_code=500)
    return _jsonable(payload)


# ==========================================================================
# V12-16 / V12-14 新增路由（追加在既有路由之后，**不修改**任何既有路由）
# ==========================================================================

@app.post("/api/experiments/randomness-selfcheck")
def experiments_randomness_selfcheck(
        seed: int = Query(42),
        n_issues: Optional[int] = Query(None, ge=0, le=20000),
        n_perm: Optional[int] = Query(None, ge=0, le=5000),
        n_draws: Optional[int] = Query(None, ge=0, le=20000),
        synthetic_bias: Optional[str] = Query(None),
        window: Optional[int] = Query(None, ge=1, le=20000)):
    """**用户可自证实验**（V12-16 / L3-7）：用合成随机数据重跑同一套检验，并排展示。

    真实数据走 ``rigor.randomness_report()`` 原样；合成数据由 ``seed`` 派生的
    ``numpy.random.default_rng`` 新生成（同期数、同蓝球范围）。返回 4 项检查
    （``real_not_detected`` / ``synthetic_uncorrected_rate_in_ci`` /
    ``strongest_p_same_magnitude`` / ``reproducible_same_seed``）、``all_pass``、
    ``seed``、``response_sha256``、``elapsed_ms``。

    可复现（A7）：同 ``seed`` 两次响应的 ``response_sha256`` 必须相同
    （哈希覆盖剔除 ``response_sha256`` 与 ``elapsed_ms`` 后的规范化 JSON）。

    参数
    ----
    seed: 唯一随机性来源（默认 42）。
    n_issues: 合成数据期数；默认 = 真实有效期数（保证同期数，方案上限 3516）。
    n_perm: 零分布重采样次数；默认 200。越小越快，越大分位估计越稳。
    n_draws: 只用最近 n 期真实数据（默认全部）；用于快速演示/自测。
    synthetic_bias: 传 ``"number"`` 时给**合成数据**注入偏置（反向演示：
        假数据一旦带偏，检查 1 会失败）。默认不注入。
    window: 兼容参数，当前未使用（保留签名以免前端改造成本）。
    """
    from . import selfcheck as _sc
    try:
        draws = db.load_draws()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "读取开奖数据失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    if n_draws:
        draws = draws[-int(n_draws):]
    bias = (synthetic_bias or "").strip() or None
    if bias not in (None, "number"):
        return JSONResponse({"ok": False,
                             "error": "synthetic_bias 只支持 'number' 或留空"},
                            status_code=400)
    try:
        payload = _sc.selfcheck(
            draws, seed=int(seed),
            n_issues=(_sc.DEFAULT_N_ISSUES if n_issues is None else int(n_issues)),
            n_perm=(_sc.DEFAULT_N_PERM if n_perm is None else int(n_perm)),
            synthetic_bias=bias)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "自证实验执行失败",
                             "detail": f"{type(e).__name__}: {e}"}, status_code=503)
    return _jsonable(payload)


@app.get("/api/audit/anomaly/latest")
def audit_anomaly_latest():
    """V12-14（L3-3）：读取**最近一次**数据异常审计结果（只读，不触发计算）。

    无记录时返回 ``available=False``（HTTP 200，前端据此显示「尚未运行」），
    不返回 404 —— 404 会让前端把它当成接口故障。
    """
    from . import data_audit
    try:
        payload = data_audit.latest_audit()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"available": False, "error": "读取审计产物失败",
                             "detail": f"{type(e).__name__}: {e}"},
                            status_code=503)
    return _jsonable(payload)


@app.post("/api/audit/anomaly/run")
def audit_anomaly_run(use_llm: int = Query(1, ge=0, le=1),
                      window: int = Query(40, ge=5, le=500),
                      w_seg: int = Query(300, ge=30, le=3000),
                      n_perm: int = Query(2000, ge=200, le=20000),
                      seed: int = Query(20261018)):
    """V12-14（L3-3）：**后台**跑一次数据异常审计（立即返回 task_id，不阻塞）。

    审计流程：确定性统计 → LLM 提名（``use_llm=1``；不可用时 ``degraded=True``）
    → 代码逐条判定（``real_defect`` / ``regime_shift`` / ``noise``）→ 落盘。
    完成后 ``GET /api/audit/anomaly/latest`` 可读到结果。
    """
    draws = db.load_draws()
    if not draws:
        return JSONResponse({"ok": False, "available": True,
                             "error": "本地暂无开奖数据"}, status_code=400)
    from . import data_audit

    def _work(progress):
        return data_audit.run_audit(draws, use_llm=bool(use_llm), window=int(window),
                                    w_seg=int(w_seg), n_perm=int(n_perm),
                                    seed=int(seed), progress=progress)

    tid = _bg_run("data_audit", _work)
    return {"ok": True, "task_id": tid,
            "note_zh": ("数据审计已在后台运行（LLM 提名 + 确定性判定），"
                        "完成后 GET /api/audit/anomaly/latest 可读结果")}


# M2：后台预热 ML 模型（首个预测请求不阻塞；训练结果按数据版本缓存）
try:
    from . import ml_model
    _boot_draws = db.load_draws()
    ml_model.start_ml_warmup(_boot_draws)
except Exception:  # noqa: BLE001
    pass

# V8：启动后异步刷新一次规律快照，保证「本期规律使用情况」不会因为镜像重建而读到陈旧快照。
# 失败只记录日志，绝不阻塞启动。
def _boot_refresh_rules_snapshot():
    import time as _t
    _t.sleep(20)                     # 让启动回测/预测先跑完，避免抢 IO
    try:
        from . import hypothesis, patterns_runtime
        _d = db.load_draws()
        if not _d:
            return
        # 假说库是运行时状态，重启/误清后会变成空库；空库会让规律链路"看起来
        # 什么都没分析"。启动时补一次统计挖掘（纯确定性、无 LLM、秒级），
        # 把重号/邻号/隔期/奇偶/三区这几类基线规律重新拉起来。
        try:
            _reg = hypothesis.load_registry()
            if not _reg.get("hypotheses"):
                _mined = hypothesis.mine_hypotheses({}, _d, use_llm=False)
                _reg_res = hypothesis.register_hypotheses(_mined.get("hypotheses") or [])
                print("[rules] 假说库为空，已补跑统计挖掘:", _reg_res.get("registered"))
                evolution_annotate = None
                try:
                    from . import evolution as _evo
                    evolution_annotate = _evo.annotate_hypotheses(_d)
                except Exception:  # noqa: BLE001
                    evolution_annotate = None
                if evolution_annotate is not None:
                    print("[rules] 阶段标注:", evolution_annotate.get("annotated"))
        except Exception as e:  # noqa: BLE001
            print("[rules] 补跑统计挖掘失败（不影响出号）:", e)
        r = patterns_runtime.refresh(_d)
        print("[rules] 启动后规律快照刷新:", (r.get("snapshot") or {}).get("counts"))
    except Exception as e:  # noqa: BLE001
        print("[rules] 启动后规律快照刷新失败（不影响出号）:", e)


# 启动后异步补刷规律快照。**必须可关闭**：
#   ① pytest 会话会反复 import 本模块，若每次都在导入期起线程，
#      多个 boot-rules-refresh 线程会并发连同一个 SQLite 连接（check_same_thread=False），
#      与 joblib 的多进程后端叠加后实测触发 **Segmentation fault**（2026-10-18）；
#   ② 调度器/CLI 等非 Web 场景也不需要这个补刷。
# 因此由 config.BOOT_REFRESH_RULES 控制（默认 1=开，pytest 下由 conftest 置 0）。
if getattr(config, "BOOT_REFRESH_RULES", True) and os.environ.get("LOTT_BOOT_REFRESH_RULES", "1") == "1":
    try:
        import threading as _threading
        _threading.Thread(target=_boot_refresh_rules_snapshot, daemon=True,
                          name="boot-rules-refresh").start()
    except Exception:  # noqa: BLE001
        pass

    # V12-16：预热自证实验的零分布（纯随机数据下「最强校正 p」的 5% 分位）。
    # 这个值是**确定性**的（只由 seed / 期数 / n_perm / 检验数 / alpha 决定），
    # 首次计算约 5.6s（200 次 × randomness_report），之后走磁盘缓存 <0.1s。
    # 把首次计算挪到启动后台，用户点「🧪 验证」时就不会等 5 秒。
    # 预热失败不影响任何功能（自证实验会自己再算一次）。
    def _boot_warm_selfcheck_null():
        import time as _t
        _t.sleep(25)          # 让启动回测/预测先跑完，避免抢 CPU（与上面补刷错开）
        try:
            from . import selfcheck as _sc
            d = db.load_draws()
            if not d:
                return
            res = _sc.warm_null_quantile(seed=_sc.DEFAULT_SEED,
                                         n_issues=len(d))
            print("[selfcheck] 零分布预热:", res)
        except Exception as e:  # noqa: BLE001
            print("[selfcheck] 零分布预热失败（不影响功能）:", e)

    try:
        import threading as _threading2
        _threading2.Thread(target=_boot_warm_selfcheck_null, daemon=True,
                           name="boot-selfcheck-warm").start()
    except Exception:  # noqa: BLE001
        pass

if config.SCHEDULER_ENABLED:
    import threading
    threading.Thread(target=_scheduler_loop, daemon=True).start()