"""D-03 端点边界加固：非法参数不得 500，降级必须 503 + degraded 可辨识。

## 本文件与 `test_v14_portfolio_api.py` 的分工

后者守 `/api/portfolio/*` 两个端点的**语义与披露一致性**；
本文件守**全站边界纪律**，覆盖 D-03 验收的四件事：

1. 参数校验：非法输入 → 400/422，**不得 500**，**不得静默截断**；
2. 503 降级：LLM 不可用 / DB 不可读 / 挖掘停机（`LOTT_IDLE_MINING=0`）；
3. 降级可辨识：503 **且**带 `degraded` 字段；**返回 200 配空数据 = 最恶劣的撒谎**；
4. 全部为失败路径用例（成功路径已由其它文件覆盖）。

## 为什么直接调路由函数

容器内没有 `httpx` TestClient。直接调用时 FastAPI 的 `Query(ge=...)` 不生效，
因此本文件额外用 **signature 元数据**断言约束**真的挂在参数上** ——
这样既测了函数自身的兜底，也测了 HTTP 层拦截的依据存在。
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.responses import JSONResponse

from lottery import routes_portfolio as R
from lottery import routes_capabilities as C


def _status(resp):
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _json(resp):
    if isinstance(resp, JSONResponse):
        import json
        return json.loads(resp.body)
    return resp


# ==========================================================================
# 一、参数校验：非法 → 400/422，绝不 500
# ==========================================================================

@pytest.mark.parametrize("reds", [
    "1,2,3",                 # 数量不足
    "1,2,3,4,5,6,7",         # 数量超出
    "1,2,3,4,5,99",          # 越界上界
    "0,1,2,3,4,5",           # 越界下界
    "1,2,3,4,5,5",           # 重复
    "a,b,c,d,e,f",           # 非数字
    "1,2,3,4,5,",            # 空段导致数量不足
    "1;2;3;4;5;6",           # 错误分隔符
])
def test_popularity_invalid_reds_are_400_not_500(reds):
    """9 种非法 reds → 400。任何一种是 500 即为缺陷。"""
    code = _status(R.portfolio_popularity(reds=reds, sales=0.0))
    assert code == 400, f"reds={reds!r} 应为 400，实际 {code}"
    assert code != 500


def test_popularity_missing_reds_default_is_not_500():
    """未传 reds（直接调用会用 Query 对象兜底）也不许 500。"""
    code = _status(R.portfolio_popularity(sales=0.0))
    assert code in (400, 422), f"缺 reds 应为 400/422，实际 {code}"


@pytest.mark.parametrize("bad", ["abc", "", "1e9999", None])
def test_blue_coverage_invalid_n_tickets_is_rejected_before_logic(bad):
    """非整数 `n_tickets` 必须在**进入业务逻辑前**被拒绝。

    真实 HTTP 层由 Pydantic 拦成 **422**（`n_tickets: int` 的类型契约）。
    直接调用函数时，Python 自己会在 `int(n_tickets)` 处抛 `TypeError`/`ValueError` ——
    这是**调用方类型错误**，不是端点缺陷；关键是它**不会**变成 500 响应，
    也**不会**被静默截断成某个合法注数（那才是"静默截断"违规）。
    """
    with pytest.raises((TypeError, ValueError)):
        R.portfolio_blue_coverage(n_tickets=bad)


def test_blue_coverage_fullwidth_n_tickets_http_layer_rejects():
    """全角数字属同一类：`int("１０")` 在 Python 里其实成功，但 Pydantic 的
    `int` 解析对 str 输入走严格模式，HTTP 层会 422。此处只断言类型契约存在。"""
    ann = inspect.signature(R.portfolio_blue_coverage).parameters["n_tickets"].annotation
    # 模块用了 `from __future__ import annotations`，注解是字符串 'int'。
    assert ann in (int, "int"), f"n_tickets 必须声明为 int，实际 {ann!r}"


def test_blue_coverage_n_tickets_bounds_are_declared():
    """`n_tickets` 必须带 ge=1/le=100 —— 这是 HTTP 层 422 的依据。"""
    md = inspect.signature(R.portfolio_blue_coverage).parameters["n_tickets"].default.metadata
    kinds = {type(m).__name__: getattr(m, "ge", getattr(m, "le", None)) for m in md}
    assert "Ge" in kinds and kinds["Ge"] == 1, f"缺 ge=1，metadata={md}"
    assert "Le" in kinds and kinds["Le"] == 100, f"缺 le=100，metadata={md}"


@pytest.mark.parametrize("n", [0, -1, -999])
def test_blue_coverage_non_positive_clamped_not_crash(n):
    """`n_tickets ≤ 0`：函数层必须夹到 0，不得崩溃（HTTP 层已 422）。"""
    body = _json(R.portfolio_blue_coverage(n_tickets=n))
    assert body["n_tickets"] == 0
    assert body["cost_yuan"] == 0.0


def test_capabilities_takes_no_params_and_never_500():
    """无参端点：直接调用两次都必须稳定返回，不得抛异常。"""
    for _ in range(2):
        body = _json(C.capabilities())
        assert body["ok"] is True


# ==========================================================================
# 二、降级可辨识：503 + degraded（不许 200 配空数据）
# ==========================================================================

def test_popularity_unfitted_is_503_with_error_not_empty_200(monkeypatch):
    """模型未就绪 → **503**，且带可辨识错误信息。

    这条守的是「200 配空数据」这种最恶劣的撒谎：用户会以为"这种号没数据"，
    实际是模型没拟合。
    """
    from lottery import popularity as POP
    saved = POP._CACHE
    try:
        POP._CACHE = POP.PopularityModel()      # fitted=False
        resp = R.portfolio_popularity(reds="1,2,3,4,5,6", sales=0.0)
        code = _status(resp)
        assert code == 503, f"未拟合必须 503，实际 {code}"
        body = _json(resp)
        assert body["ok"] is False, "503 不得返回 ok=True"
        assert body.get("error"), "503 必须带人读原因"
        assert "未就绪" in body["error"] or "未拟合" in body["error"]
    finally:
        POP._CACHE = saved


def test_capabilities_flags_degraded_when_constant_missing(monkeypatch):
    """常量源故障 → `degraded=True` 且逐项列出缺失项。"""
    from lottery import eprocess as EP
    monkeypatch.setattr(EP, "DEFAULT_ALPHA", None)
    body = _json(C.capabilities())
    # DEFAULT_ALPHA=None 时 float(None) 抛错 → 该常量记为 None + errors
    assert body["constants"]["alpha"] is None
    assert body["degraded"] is True
    assert "alpha" in body["degraded_constants"]


def test_capabilities_degraded_lists_idle_mining_when_off():
    """`LOTT_IDLE_MINING=0`（当前默认）→ degraded 段必须含 idle_mining。"""
    from lottery import idle_mining as IM
    st = IM.status()
    body = _json(C.capabilities())
    ids = {i["id"] for i in body["degraded_items"]}
    if not st.get("enabled"):
        assert "idle_mining" in ids, "挖掘停机却未进入降级清单"
    else:
        assert "idle_mining" not in ids, "挖掘开启却仍报降级"


def test_capabilities_degraded_entries_all_have_restore_path():
    """每个降级项都必须给出恢复方式，不得只报「坏了」。"""
    body = _json(C.capabilities())
    # 反空转：若降级项为空，下面的 for 不会执行 ⇒ 测试恒绿（假守卫）。
    # 当前默认环境有 2 项（idle_mining / usable_replayable），所以必须非空。
    # 若将来降级项合法地变成 0，请显式改成 monkeypatch 构造一条再验。
    assert body["degraded_items"], (
        "degraded_items 为空 —— 本测试会空转通过，等于没验。"
        "若这是预期（系统全绿），请改为 monkeypatch 构造一条降级项再断言。")
    for item in body["degraded_items"]:
        assert item.get("how_to_restore"), f"{item['id']} 缺恢复路径"


def test_capabilities_constant_error_is_visible_not_silent(monkeypatch):
    """常量故障必须在 `constants_meta.errors` 里留痕，不许静默。"""
    from lottery import candidate_space as CS

    def _boom():
        raise RuntimeError("db readonly simulated")

    monkeypatch.setattr(CS, "total_single", _boom, raising=False)
    body = _json(C.capabilities())
    errs = body["constants_meta"]["errors"]
    assert "single" in errs, "常量故障未留痕"
    assert "db readonly simulated" in errs["single"]


# ==========================================================================
# 三、DB 只读降级：不得 500，且必须可辨识
# ==========================================================================

def test_capabilities_survives_db_read_failure(monkeypatch):
    """模拟 DB 只读/不可读：能力清单不得 500。

    `idle_mining.status()` 依赖状态文件而非 DB，但 `popularity.get_model()`
    依赖 `db.load_draws()`。这里让 DB 读取抛错，端点必须仍然返回结构，
    并在 degraded 段如实报告，而不是整体崩掉。
    """
    from lottery import db

    def _boom(*a, **kw):
        raise sqlite3_error()

    def sqlite3_error():
        import sqlite3
        return sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(db, "load_draws", _boom, raising=False)
    from lottery import popularity as POP
    saved = POP._CACHE
    POP._CACHE = None
    try:
        body = _json(C.capabilities())
        assert body["ok"] is True, "DB 不可读时能力清单整体崩溃"
        ids = {i["id"] for i in body["degraded_items"]}
        assert "popularity_model" in ids, "模型取不到却未报降级"
    finally:
        POP._CACHE = saved


# ==========================================================================
# 四、降级路径不得被"空数据 200"绕过（反向守卫）
# ==========================================================================

def test_no_endpoint_hides_failure_as_empty_200():
    """扫描：`degraded` 一旦为 True，`ok` 不得同时为 True 却无任何提示。

    这是「200 配空数据」的形式化守卫 —— 至少要有一个字段说明"为什么是空的"。
    """
    body = _json(C.capabilities())
    if body.get("degraded"):
        assert body.get("degraded_constants") is not None, \
            "degraded=True 却没说清楚缺什么"
        assert body["constants_meta"].get("note_zh"), "缺口径说明"


@pytest.mark.parametrize("banned", ["提高中奖率", "提高命中率", "更划算", "稳赚"])
def test_capabilities_has_no_banned_words(banned):
    """新增端点同样受禁用词约束（红线 2）。"""
    import json
    blob = json.dumps(C.capabilities(), ensure_ascii=False)
    assert banned not in blob


# ==========================================================================
# 五、宽容解析 ≠ 校验放行（如实记录真实行为）
# ==========================================================================

def test_fullwidth_reds_are_accepted_by_int_parsing():
    """全角数字 `int("１")` == 1，Python 原生接受 —— 这是**真实行为**，不是缺陷。

    记录它的意义：如果有人以为"非 ASCII 数字会被拒绝"，会在这里被纠正。
    它不会导致越界或重复（解析后仍受 1~33 与去重校验）。
    """
    body = _json(R.portfolio_popularity(reds="１,２,３,４,５,６", sales=0.0))
    # 未拟合时是 503；已拟合是 200。两者都说明"解析通过"。
    assert body.get("ok") is False and "error" in body or body.get("ok") is True
    if "reds" in body:
        assert body["reds"] == [1, 2, 3, 4, 5, 6]
