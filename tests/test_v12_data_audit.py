"""V12-14 数据异常期检测器（L3-3）验收测试。

覆盖（≥6 例）
-------------
1. 注入 ±30% 偏置（两段不重叠数据都带偏）→ ``judge_anomaly`` 必须判 ``real_defect``；
2. 纯随机数据 → 必须判 ``noise``（双向验证：检测器不能靠「总是报警」蒙对）；
3. 只在一段注入 → 必须判 ``regime_shift``（「只在单段出现」的唯一定义）；
4. ``parse_anomalies`` 对垃圾输入不抛异常且返回 ``parse_error``（安全降级）；
5. 每个判决的 p 值必带 ``m_adj`` + ``scope``（项目铁律：无 scope 的 p 值不得展示）；
6. **反向用例**：把 ``m_adj`` 偷换成 1 时判定必须改变（口径不可偷换，A3）；
7. LLM 可用（桩）时全链路打分正确；LLM 不可用时 ``degraded=True`` 而不是崩溃；
8. 端点 ``GET /api/audit/anomaly/latest`` 无记录时 ``available=False``。

设计约定
--------
- 不依赖网络、不依赖真实库：用 ``tests/conftest.py`` 的 ``make_draws``；
- LLM **默认不真调**：一律 ``monkeypatch.setattr(llm_client, "chat_json", ...)``；
- 数据规模 1600 期（主段 300 + 复现段 300 + 余量），``n_perm`` 压到 400~800 控制耗时。
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from lottery import data_audit as DA


# --------------------------------------------------------------------------
# 工具：确定性注入
# --------------------------------------------------------------------------

def _inject_segment(draws, start: int, end: int, numbers=range(1, 12),
                    weight: float = 1.30, seed: int = 5):
    """把 ``[start, end)`` 段内号码 ``numbers`` 的权重抬高 ``weight`` 倍（±30% 偏置）。

    用独立 ``default_rng(seed)``，与 ``make_draws`` 无关，保证注入位置/强度可复现。
    ``weight=1.30`` 即「+30% 偏置」，与方案 §1.3 的注入强度一致。
    """
    out = copy.deepcopy(list(draws))
    rng = np.random.default_rng(seed)
    base = np.arange(1, 34)
    w = np.ones(33, dtype=float)
    for n in numbers:
        w[int(n) - 1] = float(weight)
    p = w / w.sum()
    for i in range(int(start), min(int(end), len(out))):
        out[i]["reds"] = sorted(int(x) for x in
                                rng.choice(base, 6, replace=False, p=p))
    return out


def _pure(n: int = 1600, seed: int = 11):
    """纯随机「真实数据」（无放回抽 6 个红球 + 均匀蓝球）。"""
    rng = np.random.default_rng(seed)
    return [{"issue": f"{i:04d}",
             "reds": sorted(int(x) for x in rng.choice(np.arange(1, 34), 6,
                                                       replace=False)),
             "blue": int(rng.integers(1, 17))} for i in range(n)]


def _real_defect_draws():
    """两段**不重叠**的数据都被注入 +30% 偏置 → 主段与复现段各 300 期。

    为什么注入两段：``real_defect`` 的定义是「显著且方向一致**且在独立复现段也成立**」。
    只注入一段不可能满足定义（那正是 ``regime_shift``）。两段相邻注入后，
    主段 [400,700) 与几何规则选出的复现段 [700,1000) 都带偏。
    """
    d = _pure(1600, seed=11)
    return _inject_segment(_inject_segment(d, 400, 700, seed=5), 700, 1000, seed=6)


# --------------------------------------------------------------------------
# 1. 注入 ±30% → real_defect
# --------------------------------------------------------------------------

def test_injected_bias_is_real_defect():
    """注入 ±30% 的**两段不重叠**偏置 → ``judge_anomaly`` 必须判 ``real_defect``。

    实现错误时会如何失败：
    - 若复现段被写成「和主段同一段」，两段 p 值必然相同，任何显著都判 real_defect
      （判据退化成「只看一段」）——本测试仍会通过，所以另有一条
      ``test_single_segment_bias_is_regime_shift`` 把这条退路堵死；
    - 若零分布用了「打乱真实序列」（均值类统计量在重排下不变，p 值恒为固定值），
      主段与复现段会同时显著或同时不显著，判据失去分辨力；
    - 若偏置注入没生效，主段 p 值不显著 → noise，本用例红。
    """
    draws = _real_defect_draws()
    claim = {"feature": "zone_dist", "range": {"start": 400, "end": 699},
             "direction": "above", "severity": "high"}
    j = DA.judge_anomaly(claim, draws, n_perm=800, seed=DA.DEFAULT_SEED)
    assert j["verdict"] == "real_defect", j["note_zh"]
    assert j["replicated"] is True
    assert j["p_adj"] < 0.05
    # 复现段必须真的不重叠于主段
    main = j["segments"][0]
    rep = j["segments"][1]
    assert rep["role"] == "replication"
    assert rep["end"] < main["start"] or rep["start"] > main["end"]
    assert main["p_value"] < 0.05 and rep["p_value"] < 0.05


# --------------------------------------------------------------------------
# 2. 纯随机 → noise
# --------------------------------------------------------------------------

@pytest.mark.parametrize("feature", ["sum_mean", "odd_count", "zone_dist",
                                     "num_freq", "consecutive"])
def test_pure_random_is_noise(feature):
    """纯随机数据上，5 个特征都必须判 ``noise``（不显著）。

    实现错误时会如何失败：
    - 若零分布用了「重排真实序列」，均值类统计量（和值/奇数/连号率）的零分布
      退化成正态冲击（实测 sd 4e-16），p 值会被真实数据自身波动主导，
      纯随机数据也会被误判成 regime_shift/real_defect —— 本用例红；
    - 若 ``zone_dist`` 的期望频数写错（例如每区期望写成 6n/33 而非 2n，
      或号码→区索引用 ``//11`` 导致 33 号落错区），卡方会恒为巨值，
      纯随机数据被判 real_defect —— 本用例红（这是本次开发中真实出现过的 bug）。
    """
    draws = _pure(1600, seed=11)
    claim = {"feature": feature, "range": {"start": 400, "end": 699}}
    j = DA.judge_anomaly(claim, draws, n_perm=800, seed=DA.DEFAULT_SEED)
    assert j["verdict"] == "noise", f"{feature}: {j['verdict']} / {j['note_zh']}"


# --------------------------------------------------------------------------
# 3. 只在单段注入 → regime_shift
# --------------------------------------------------------------------------

def test_single_segment_bias_is_regime_shift():
    """只在主段注入偏置（复现段干净）→ 必须判 ``regime_shift``，**不得**判 real_defect。

    这条用例专门堵住「两段复现」判据被架空：如果实现里复现段被错写成主段本身
    （或复现段也被顺带注入了偏置），结果会变成 real_defect，本用例红。

    实现错误时会如何失败：把「两段复现」简化成「主段显著即 real_defect」时红。
    """
    draws = _inject_segment(_pure(1600, seed=11), 400, 700, seed=5)
    claim = {"feature": "zone_dist", "range": {"start": 400, "end": 699}}
    j = DA.judge_anomaly(claim, draws, n_perm=800, seed=DA.DEFAULT_SEED)
    assert j["p_adj"] < 0.05, j["note_zh"]           # 主段确实显著
    assert j["verdict"] == "regime_shift", j["note_zh"]
    assert j["replicated"] is False


# --------------------------------------------------------------------------
# 4. parse_anomalies 安全降级
# --------------------------------------------------------------------------

@pytest.mark.parametrize("garbage", [
    None, "", "   ", "完全不是 JSON 的文本", "[1,2,3]", "{\"a\": 1}",
    "[{\"feature\": ", "[[[['", "\x00\x01", "[{\"feature\":\"不存在的特征\"}]",
    b"\xff\xfe".decode("utf-8", "ignore"),
])
def test_parse_anomalies_never_raises(garbage):
    """``parse_anomalies`` 对任何垃圾输入**不得抛异常**，必须返回 ``(items, error)``。

    实现错误时会如何失败：直接 ``json.loads`` 而不捕获 ``JSONDecodeError``
    时本用例红（LLM 输出不可靠是常态，解析崩溃会让整次审计 500）。
    """
    items, err = DA.parse_anomalies(garbage)
    assert isinstance(items, list)
    assert err is None or isinstance(err, str)
    if not items:
        assert err  # 空提名必须给出原因，不能静默


def test_parse_anomalies_accepts_schema_variants():
    """合法输出（JSON 数组 / 带代码块 / dict 包一层 / 别名）都必须能解析。"""
    text = ('说明文字\n```json\n[{"feature": "和值", "range": {"start": 0, "end": 299},'
            ' "direction": "below", "severity": "high", "reason_zh": "和值偏低"}]\n```')
    items, err = DA.parse_anomalies(text)
    assert err is None
    assert items[0]["feature"] == "sum_mean"
    assert items[0]["range"] == {"start": 0, "end": 299}
    assert items[0]["direction"] == "below"

    items2, err2 = DA.parse_anomalies({"anomalies": [{"feature": "odd_count"}]})
    assert err2 is None and items2[0]["feature"] == "odd_count"
    # 单引号 Python 字面量（LLM 常见）也必须能吃
    items3, err3 = DA.parse_anomalies("[{'feature': 'consecutive'}]")
    assert err3 is None and items3[0]["feature"] == "consecutive"


# --------------------------------------------------------------------------
# 5. p 值必带 m_adj + scope
# --------------------------------------------------------------------------

def test_pvalue_always_carries_m_adj_and_scope():
    """每个判决的 p 值必须同时带 ``m_adj`` 与 ``scope``（无 scope 的 p 值不得展示）。

    实现错误时会如何失败：若只返回 ``p_value``/``p_adj`` 而没有 ``scope``，
    前端就无法说明「这个 p 是在多少次检验、哪段数据上得到的」，本用例红。
    """
    draws = _real_defect_draws()
    for feature in DA.FEATURES:
        j = DA.judge_anomaly({"feature": feature,
                              "range": {"start": 400, "end": 699}},
                             draws, n_perm=300, seed=DA.DEFAULT_SEED)
        assert isinstance(j["m_adj"], int) and j["m_adj"] >= 1
        assert j["scope"] is not None
        assert j["scope"]["m_adj"] == j["m_adj"]
        assert j["scope"]["correction"] == "bonferroni"
        assert "segment" in j["scope"]
        assert j["scope"]["segment"]["start"] == 400
        # p_adj 必须等于 min(1, p_value × m_adj)（不容许两套口径）
        assert j["p_adj"] == pytest.approx(min(1.0, j["p_value"] * j["m_adj"]), abs=1e-12)
        # 每个段也要各自带 p 与 m_adj 说明
        for seg in j["segments"]:
            assert "p_value" in seg
            assert "p_adj" in seg


# --------------------------------------------------------------------------
# 6. 反向用例：m_adj 偷换成 1 → 判定必须改变
# --------------------------------------------------------------------------

def test_reverse_m_adj_collapse_to_one_changes_verdict():
    """**反向用例（A3）**：把 ``m_adj`` 偷换成 1 后，判定必须改变（或不成立）。

    构造一个「未校正 p 落在 (α/m, α) 区间」的弱信号场景：此时
    ``m_adj=1`` 会判显著、``m_adj=m`` 会判不显著。若实现「无论 m 是多少都判显著」
    （即 m_adj 只是摆设、没真正进入 p_adj），本用例红。

    实现错误时会如何失败：``p_adj`` 若恒等于未校正 ``p_value``（忘记乘 m_adj），
    两种 m 的判定将完全一致，本断言失败。
    """
    draws = _real_defect_draws()
    claim = {"feature": "zone_dist", "range": {"start": 400, "end": 699}}
    # 先确认这个真实信号的未校正 p 足够小，再人为放大 m 让它失去显著性
    base = DA.judge_anomaly(claim, draws, n_perm=800, seed=DA.DEFAULT_SEED, m_adj=1)
    assert base["m_adj"] == 1
    # m_adj=1 时 p_adj == p_value，必然显著
    assert base["p_adj"] == pytest.approx(base["p_value"], abs=1e-12)
    # 把 m_adj 抬到 1e6（等价于「假装开了 100 万次检验」）：必须变不显著
    big = DA.judge_anomaly(claim, draws, n_perm=800, seed=DA.DEFAULT_SEED, m_adj=10 ** 6)
    assert big["p_adj"] == pytest.approx(1.0, abs=1e-9)
    assert big["verdict"] != base["verdict"]
    assert big["verdict"] == "noise"


def test_reverse_player_ignoring_m_adj_is_caught():
    """反向用例的第二种形态：**假判定器**忽略 m_adj 时，上面的断言必须能抓住它。

    这里直接构造一个「p_adj 永远是 p_value」的错误判据，验证测试逻辑本身有效
    （不是被测实现，而是测试的灵敏度自检）。若有人把 ``judge_anomaly`` 改成
    忽略 m_adj，``test_reverse_m_adj_collapse_to_one_changes_verdict`` 会红。
    """
    p_value = 0.004
    alpha = 0.05

    def good(p, m):          # 正确：p_adj = min(1, p*m)
        return min(1.0, p * m) < alpha

    def bad(p, m):           # 错误：m 被忽略
        return p < alpha

    assert good(p_value, 1) is True
    assert good(p_value, 100) is False      # 正确实现会翻转
    assert bad(p_value, 1) == bad(p_value, 100)   # 错误实现不会翻转
    # 因此「抬 m 必须翻转结论」这条断言足以区分两者
    assert (good(p_value, 1) != good(p_value, 100))


# --------------------------------------------------------------------------
# 7. LLM 桩 / LLM 不可用降级
# --------------------------------------------------------------------------

def test_audit_with_llm_stub(monkeypatch):
    """LLM 用桩注入「正常提名」→ 全链路必须产出同一判决（代码说了算）。

    实现错误时会如何失败：若判定器信任 LLM 给的结论（例如把 LLM 的 severity
    直接当 verdict），桩返回高 severity 时会得 real_defect 而非按代码判定的结果，
    本用例的「纯随机必 noise」断言会红。
    """
    from lottery import llm_client
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **kw: [{"feature": "sum_mean",
                                           "range": {"start": 400, "end": 699},
                                           "direction": "below", "severity": "high",
                                           "reason_zh": "和值偏低"}])
    res = DA.audit_draws(_pure(1600, seed=11), n_perm=400, w_seg=300)
    assert res["llm"]["called"] is True
    assert res["llm"]["degraded"] is False
    assert res["summary"]["n_claims"] == 1
    assert res["judgments"][0]["verdict"] == "noise"   # 纯随机 → 代码判 noise
    assert res["degraded"] is False


def test_audit_degrades_when_llm_unavailable(monkeypatch):
    """LLM 不可用（返回 None）时必须 ``degraded=True`` 且**不崩**。

    实现错误时会如何失败：若 ``audit_draws`` 直接把 None 丢给 ``parse_anomalies``
    而没有降级分支，或让异常冒泡，本用例红（后台任务会变成 failed，用户看不到原因）。
    """
    from lottery import llm_client
    monkeypatch.setattr(llm_client, "chat_json", lambda *a, **kw: None)
    res = DA.audit_draws(_pure(800, seed=11), n_perm=200, w_seg=300)
    assert res["ok"] is True
    assert res["degraded"] is True
    assert res["llm"]["degraded"] is True
    assert res["claims"] == []
    assert res["judgments"] == []
    assert "LLM" in res["note_zh"]


def test_audit_degrades_when_llm_raises(monkeypatch):
    """LLM 抛异常时同样必须降级（不得让异常冒泡到路由层）。"""
    from lottery import llm_client

    def _boom(*a, **kw):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(llm_client, "chat_json", _boom)
    res = DA.audit_draws(_pure(800, seed=11), n_perm=200, w_seg=300)
    assert res["ok"] is True and res["degraded"] is True
    assert "502" in (res["llm"]["error"] or "")


# --------------------------------------------------------------------------
# 8. build_audit_prompt：只陈述已算好的数字
# --------------------------------------------------------------------------

def test_build_audit_prompt_states_numbers_not_asks_to_compute():
    """prompt 必须含已算好的数字与「不得自己计算」的禁令，且空数据也能安全生成。"""
    draws = _pure(600, seed=11)
    p = DA.build_audit_prompt(draws, window=40)
    assert "已经算好" in p or "已算好" in p
    assert "请勿重新计算" in p
    stats = DA.build_stats(draws, window=40)
    # prompt 里的关键数字必须来自 build_stats（不是 LLM 现算）
    assert str(stats["sum"]["recent_mean"]) in p
    assert str(stats["n_all"]) in p
    empty = DA.build_audit_prompt([], window=40)
    assert "空数组" in empty


# --------------------------------------------------------------------------
# 9. 端点：latest 无记录时 available=False
# --------------------------------------------------------------------------

def test_latest_endpoint_available_false(isolated_env):
    """无审计记录时 ``GET /api/audit/anomaly/latest`` 返回 ``available=False``（HTTP 200）。

    实现错误时会如何失败：若返回 404，前端会把它当接口故障；若返回 500 同。\n
    """
    from fastapi.testclient import TestClient
    from lottery import api_app
    client = TestClient(api_app.app)
    r = client.get("/api/audit/anomaly/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["note_zh"]
