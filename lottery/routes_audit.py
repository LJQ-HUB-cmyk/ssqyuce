"""审计与自证域路由（AI 两岗位审计 / L2 精确分布 / 自证实验 / 数据异常检测）。

P2 模块化：从 `api_app.py` 末尾整块切出。

## 铁律（V10 §3.2，必须保留在代码里）

AI 两岗位的路由**不产号、不写 predictions、不进候选池**；只读 `llm_audit`
（仅追加）或把判定扔进后台任务。无记录时返回 `available=False` ——
前端必须显示「本次未取得 AI 核对结果」，**不许**渲染成「AI 已核对」。

这条不只是风格问题：把"没核对"显示成"已核对"，是这个系统最不能犯的错
—— 它让用户以为有额外保障，而实际没有。

## 为什么这几块放在一起

它们都是**只读的核对/自证工具**：不改变出号、用户不直接依赖。
共同点是回答「你说的是真的吗」这一类问题。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from . import config, db
from .bg_tasks import _bg_run
from .jsonutil import jsonable as _jsonable

router = APIRouter()

# ---------------------------------------------------------------------------
# V12-13：AI 两岗位（核对员 / 契约检查器）+ 打分卡
#
# 铁律（V10 §3.2 两岗位共同约束）：这两条路由**不产号、不写 predictions、
# 不进候选池**；只读 ``llm_audit``（仅追加）或把判定扔进后台任务。
# 无记录时 ``available=False`` —— 前端必须显示"本次未取得 AI 核对结果"，
# **不许**渲染成"AI 已核对"。
# ---------------------------------------------------------------------------

@router.get("/api/llm/audit/latest")
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


@router.post("/api/llm/audit/run")
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

@router.get("/api/l2/distribution")
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


@router.get("/api/portfolio/marginal")
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

@router.post("/api/experiments/randomness-selfcheck")
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


@router.get("/api/audit/anomaly/latest")
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


@router.post("/api/audit/anomaly/run")
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
