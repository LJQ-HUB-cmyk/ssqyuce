"""V12-16 用户可自证实验（L3-7）验收测试 —— A7「同 seed 逐字节一致」。

覆盖
----
1. 同 seed 两次调用 ``response_sha256`` 相等（A7）；
2. 4 项检查全 ``pass``，字段齐全且每项带 ``note_zh``；
3. 换 seed 后结果不同（说明 seed 真的参与了计算，而不是被忽略）；
4. **反向用例**：合成数据注入偏置 → 检查项 1 必须失败；
5. 检查项④的自哈希确实覆盖 ``elapsed_ms`` 以外的全部字段（改一个字段 ⇒ sha 变）；
6. 端点层 A7：经 FastAPI ``TestClient`` 两次 POST 得到同一 sha。

设计约定
--------
- 不依赖网络、不依赖真实库：全部用 ``tests/conftest.py`` 的 ``make_draws`` 合成数据；
- 不裸赋值 ``config.DB_PATH``（conftest 已 autouse 隔离）；
- 用 ``n_issues``/``n_perm`` 把规模压到 ≤ 600 期 / 40 次，保证单测秒级。
"""
from __future__ import annotations

import json

import pytest

from lottery import selfcheck as SC


# --------------------------------------------------------------------------
# 1. A7：同 seed 两次 sha256 相等
# --------------------------------------------------------------------------

def test_same_seed_same_sha(make_draws):
    """同 seed 两次调用的 ``response_sha256`` 必须逐位相同（A7 的模块层验收）。

    实现错误时会如何失败：一旦链路里混入时钟、``random`` 全局状态、``uuid``、
    ``id()`` 或依赖 dict 插入序的浮点累加，两次摘要就会分叉，本用例立刻红。
    """
    draws = make_draws(600, seed=11)
    a = SC.selfcheck(draws, seed=42, n_perm=40)
    b = SC.selfcheck(draws, seed=42, n_perm=40)
    assert a["response_sha256"] == b["response_sha256"]
    assert len(a["response_sha256"]) == 64
    # 哈希必须能独立复算：剔除 response_sha256 / elapsed_ms 后重算必须一致
    assert SC.response_sha256(a) == a["response_sha256"]
    assert SC.response_sha256(b) == b["response_sha256"]


# --------------------------------------------------------------------------
# 2. 4 项检查全 pass 且字段齐全
# --------------------------------------------------------------------------

def test_four_checks_all_pass(make_draws):
    """纯随机合成「真实数据」下 4 项检查必须全 pass，且每项带 name/pass/note_zh。

    实现错误时会如何失败：若某项误用未校正口径、或把「证据不足」判成通过、
    或某项返回缺字段，这里都会红。
    """
    draws = make_draws(600, seed=12)
    r = SC.selfcheck(draws, seed=42, n_perm=60)
    assert r["n_checks"] == 4
    assert [c["name"] for c in r["checks"]] == list(SC.CHECK_NAMES)
    for c in r["checks"]:
        assert set(("name", "pass", "real", "synthetic", "note_zh")) <= set(c)
        assert isinstance(c["pass"], bool)
        assert c["note_zh"]
    assert r["all_pass"] is True
    assert r["n_pass"] == 4
    # 检查①必须说明「合成数据也不报」，而不是只说真实数据
    assert "合成" in r["checks"][0]["note_zh"]
    # 诚实边界文案必须存在
    assert "合成" in r["honest_note_zh"] and "随机" in r["honest_note_zh"]


# --------------------------------------------------------------------------
# 3. 换 seed 结果不同
# --------------------------------------------------------------------------

def test_different_seed_differs(make_draws):
    """换一个 seed 后 ``response_sha256`` 必须不同（否则 seed 根本没进计算）。

    实现错误时会如何失败：如果合成数据用固定常量种子（忽略入参 seed），
    两次摘要会相同，本用例红 —— 这正是「可复现」被降级成「可重现同一答案」的典型。
    """
    draws = make_draws(400, seed=13)
    a = SC.selfcheck(draws, seed=42, n_perm=30)
    b = SC.selfcheck(draws, seed=43, n_perm=30)
    assert a["response_sha256"] != b["response_sha256"]
    assert a["synthetic"] != b["synthetic"]


# --------------------------------------------------------------------------
# 4. 反向用例：注入偏置 → 检查项①必须失败
# --------------------------------------------------------------------------

def test_reverse_biased_synthetic_fails_check1(make_draws):
    """**反向用例**：给合成数据注入偏置后，检查项①必须失败（all_pass=False）。

    方案要求：「把合成数据的注入偏置打开（让合成数据带偏置），检查项①必须失败」。
    它证明的是「自证实验不是走过场」——检测器对**已知的假偏置**会响，
    所以真实数据「未检出」才是有信息量的。

    实现错误时会如何失败：若 ``synthetic_bias="number"`` 被忽略（例如参数没传进
    ``synthetic_draws``，或偏置强度被调到检不出），合成数据会与真实数据一样报
    「不可区分」，检查①仍 pass，本用例红。
    """
    draws = make_draws(600, seed=14)
    for seed in (42, 43):
        r = SC.selfcheck(draws, seed=seed, n_perm=40, synthetic_bias="number")
        c1 = r["checks"][0]
        assert c1["name"] == "real_not_detected"
        assert c1["pass"] is False, f"seed={seed} 注入偏置后检查①仍通过（反向用例失效）"
        assert r["all_pass"] is False
        # 真实数据侧仍应「不可区分」——失败必须来自合成数据侧
        assert r["real"]["verdict_fwer"] == "consistent-with-random"
        assert r["synthetic"]["verdict_fwer"] == "deviation"


# --------------------------------------------------------------------------
# 5. 自哈希覆盖校验
# --------------------------------------------------------------------------

def test_sha_excludes_only_volatile_fields(make_draws):
    """``response_sha256`` 必须随任意非易变字段变化，且只剔除 elapsed_ms 与自身。

    实现错误时会如何失败：若把统计结果也排除在哈希外（例如只哈希 seed），
    篡改 ``real`` 后 sha 不变，本用例红 —— 那样的 A7 等于没验。
    """
    draws = make_draws(300, seed=15)
    r = SC.selfcheck(draws, seed=7, n_perm=20)
    base = SC.response_sha256(r)
    # 改 elapsed_ms：sha 不变（它是易变字段）
    r2 = dict(r)
    r2["elapsed_ms"] = int(r["elapsed_ms"]) + 12345
    assert SC.response_sha256(r2) == base
    # 改 real 里的一个 p 值：sha 必须变
    r3 = json.loads(json.dumps(r))
    r3["real"]["tests_p"]["red_frequency"] = 0.123456789
    assert SC.response_sha256(r3) != base


# --------------------------------------------------------------------------
# 6. 端点层 A7（两次 POST 同一 sha）
# --------------------------------------------------------------------------

@pytest.fixture()
def client(isolated_env):
    """FastAPI TestClient（conftest 已隔离 DB/DATA_DIR，无需再动 config）。"""
    from fastapi.testclient import TestClient
    from lottery import api_app
    return TestClient(api_app.app)


def test_endpoint_same_seed_same_sha(client, seeded_db):
    """端点验收：``POST /api/experiments/randomness-selfcheck?seed=42`` 两次 sha 相同。

    实现错误时会如何失败：若路由层引入了时间戳/请求级随机数（例如把 elapsed_ms
    拼进哈希密钥、或用 ``uuid`` 做缓存键参与计算），两次 POST 的 sha 会不同。
    """
    seeded_db(n=400, seed=21)
    a = client.post("/api/experiments/randomness-selfcheck?seed=42&n_perm=20")
    b = client.post("/api/experiments/randomness-selfcheck?seed=42&n_perm=20")
    assert a.status_code == 200 and b.status_code == 200
    ja, jb = a.json(), b.json()
    assert ja["response_sha256"] == jb["response_sha256"]
    assert ja["seed"] == 42
    assert ja["n_checks"] == 4
    assert "elapsed_ms" in ja


def test_endpoint_empty_db_fails_check1_not_500(client):
    """空库时端点必须 200 但检查①失败（「没检查」不得算「检查通过」）。

    实现错误时会如何失败：若把 insufficient-data 当成「不可区分」，
    检查①会 pass，本用例红；若直接抛 500，也红（前端会把它当成接口故障）。
    """
    r = client.post("/api/experiments/randomness-selfcheck?seed=42&n_issues=200&n_perm=10")
    assert r.status_code == 200
    j = r.json()
    assert j["checks"][0]["pass"] is False
    assert j["all_pass"] is False


# --------------------------------------------------------------------------
# 7. 性能护栏 + 磁盘缓存（2026-10-19 Lead 验收时发现的真实缺陷）
# --------------------------------------------------------------------------

def test_null_quantile_disk_cache_is_used_and_identical(make_draws, monkeypatch):
    """零分布**磁盘缓存**：清空进程内缓存后，结果必须逐位相同且不再重算 200 次。

    背景（真实缺陷，不是假想）：方案要求自证实验耗时 < 3s，而首次计算
    ``n_perm=200`` 组零分布实测 **5.6s**。降低 ``n_perm`` 能跑进 3s，但会把
    判据偷偷放宽（实测 5% 分位 200→0.0264 vs 40→0.0863），所以不可接受。
    正确做法是「确定性值跨进程缓存 + 启动期预热」。

    若实现错误会如何失败：
      * 缓存键漏了参数（如没有 n_perm）→ 换 n_perm 仍读到旧值 → 下面的
        ``q_other`` 断言失败；
      * 缓存读回来的值被二次加工 → 与首次值不逐位相等 → 断言失败；
      * 只在进程内缓存 → 清空 ``_NULL_Q05_CACHE`` 后又要跑 200 次
        ``randomness_report``（本用例用 monkeypatch 计数，次数 > 5 即失败）。
    """
    from lottery import rigor

    SC._NULL_Q05_CACHE.clear()
    calls = {"n": 0}
    real_report = rigor.randomness_report

    def counting_report(*a, **k):
        calls["n"] += 1
        return real_report(*a, **k)

    monkeypatch.setattr(rigor, "randomness_report", counting_report)

    # 首次：完整计算（会写磁盘缓存）
    q_first = SC._null_quantile(42, 120, 9, 8, 0.05)
    first_calls = calls["n"]
    assert first_calls > 0

    # 清进程内缓存 → 必须命中磁盘缓存，不得重算
    SC._NULL_Q05_CACHE.clear()
    calls["n"] = 0
    q_second = SC._null_quantile(42, 120, 9, 8, 0.05)
    assert q_second == q_first, "磁盘缓存读回的值必须与首次逐位相同"
    assert calls["n"] <= 1, f"命中磁盘缓存后不应重算 200 次，实际调用了 {calls['n']} 次"

    # 缓存键必须区分参数：换 n_perm 不能读到旧值
    calls["n"] = 0
    q_other = SC._null_quantile(42, 120, 9, 9, 0.05)
    assert calls["n"] > 0, "换了 n_perm 必须重新计算，不能命中旧缓存"
    assert isinstance(q_other, float)


def test_warm_null_quantile_returns_float(make_draws):
    """预热接口（启动后台线程用）必须返回可用的 q05，且不抛异常。"""
    SC._NULL_Q05_CACHE.clear()
    res = SC.warm_null_quantile(seed=7, n_issues=120, n_perm=8)
    assert res.get("cached") is True
    assert isinstance(res.get("q05"), float)
    assert 0.0 <= res["q05"] <= 1.0
