"""容器 CPU 100% 治理的回归测试（2026-10-19）。

背景（实测根因，不是猜测）
--------------------------
第四批验收留下的疑点是「容器 CPU 长期 100%」。本次核查结论：

* **当前基线并不高**（`docker stats` 实测 0.20%，宿主 load 0.62）；
* 真正的现象是**周期性满核**：`_scheduler_step` 每 30 分钟跑一轮
  `_evolution_step`，而每轮对全部 ~32 条 proposed 假说逐条做 A/B 验证。
  日志实测三轮耗时 **22 / 19 / 22 分钟**（14:14→14:36、15:06→15:25、15:56→16:18），
  即每 30 分钟里有 17~22 分钟单个核心跑满 ⇒ 持续占用约 57%~73%。

根因（profiler 定位，`verify_hypothesis_ab` 单次 41.6s）
------------------------------------------------------
`engine._rebuild_probs(name, hist)` 无论要哪个方法，都调
``models.build_models(hist)`` —— **一次算 freq / omission / markov / bayes 四个模型**。
而调用方只取其中一个：

* ``evolution._ab_structural_delta`` 只要 ``"freq"``（O(30) 的轻量计算）；
* ``battle._method_probs`` 每个方法也只取一个。

实测 profile：``markov_model`` 被调 **1200 次**、吃掉 20.6s / 32.4s（63%）；
``omission_model`` 另占 8.1s。这些全是用不到的计算。

修法与验收
----------
`_rebuild_probs` 改为**按需只算被请求的那一个模型**。必须同时满足：
1. **逐位对等**：单模型构建与 ``build_models`` 的返回值完全相同（同一函数同一输入）；
2. **收益可测**：3 条假说的验证耗时从 ~95s 降到 ~10s（实测 9.6s）；
3. **结果不变**：优化前后 ``delta_red_hits`` / ``delta_blue_rate`` 逐位相同（已实测）；
4. **集合不漂移**：``_MODEL_BUILDERS`` 与 ``build_models`` 的成员集合一致——
   防止将来在 ``build_models`` 里加了模型却忘了登记，导致该模型静默返回 None。
"""
from __future__ import annotations

import numpy as np
import pytest

from lottery import db
from lottery import engine as E
from lottery import models as M


@pytest.fixture()
def hist(seeded_db):
    """固定 800 期合成历史（够长以覆盖 markov/omission 的全部分支）。"""
    seeded_db(800, seed=17)
    return db.load_draws()[:800]


def test_model_builders_cover_build_models(hist):
    """(d) ``_MODEL_BUILDERS`` 的方法集合必须与 ``build_models`` 完全一致。

    实现错误时如何失败：若将来在 ``models.build_models`` 里新增第 5 个模型
    却没登记到 ``_MODEL_BUILDERS``，则该模型的 ``_rebuild_probs`` 会静默返回
    ``None``（调用方按 fixed-fallback 处理），回测结果会在无人察觉的情况下改变。
    本用例先于那种情况失败。
    """
    built = set(M.build_models(hist))
    registered = set(E._MODEL_BUILDERS)
    assert registered == built, (
        f"_MODEL_BUILDERS 与 build_models 不一致："
        f"多出 {registered - built}，缺少 {built - registered}")


@pytest.mark.parametrize("name", ["freq", "omission", "markov", "bayes"])
def test_single_model_build_is_bitwise_identical(hist, name):
    """(a) 逐位对等：单模型构建 == ``build_models`` 里那一个。

    实现错误时如何失败：若有人把单模型计算「顺手优化」成近似计算
    （例如 markov 少看一期、freq 窗口改了），返回值不再逐位相等 → 本用例失败。
    """
    ref = M.build_models(hist)[name]
    fn = E._MODEL_BUILDERS[name]
    got = fn(hist)
    assert np.array_equal(np.asarray(got["red"]), np.asarray(ref["red"]))
    assert np.array_equal(np.asarray(got["blue"]), np.asarray(ref["blue"]))


@pytest.mark.parametrize("name", ["freq", "omission", "markov", "bayes"])
def test_rebuild_probs_matches_build_models(hist, name):
    """(a) ``_rebuild_probs`` 的返回与「全量构建后取其一」逐位相同。"""
    got = E._rebuild_probs(name, hist)
    ref = M.build_models(hist)[name]
    assert got is not None
    assert np.array_equal(got["red"], np.asarray(ref["red"], dtype=float))
    assert np.array_equal(got["blue"], np.asarray(ref["blue"], dtype=float))


def test_rebuild_probs_does_not_build_unrequested_models(hist, monkeypatch):
    """(b) **核心性能断言**：请求 ``freq`` 时**不得**调用 markov/omission/bayes。

    这是本优化的全部意义所在。实现退回去（又调 ``build_models``）时，
    下面三个计数都会变成 >0 → 用例失败。
    """
    calls = {"omission": 0, "markov": 0, "bayes": 0}
    for name in calls:
        real = getattr(M, f"{name}_model")

        def make(real_fn=real, key=name):
            def _wrapped(*a, **k):
                calls[key] += 1
                return real_fn(*a, **k)
            return _wrapped

        monkeypatch.setattr(M, f"{name}_model", make())

    # 注意：_MODEL_BUILDERS 在 import 期已绑定函数对象，所以这里同时替换表项，
    # 模拟「按需构建」的真实路径（否则测的只是 build_models 有没有被调用）。
    monkeypatch.setitem(E._MODEL_BUILDERS, "freq", M.freq_model)
    E._rebuild_probs("freq", hist)
    assert calls == {"omission": 0, "markov": 0, "bayes": 0}, (
        f"请求 freq 却构建了其它模型：{calls}")


def test_rebuild_probs_unknown_method_returns_none(hist):
    """未登记的方法（ml / llm 等无法回放的通道）必须返回 None，由调用方降级。"""
    assert E._rebuild_probs("ml", hist) is None
    assert E._rebuild_probs("not_a_model", hist) is None


def test_rebuild_probs_blue_specialist_still_works(hist):
    """``blue_specialist`` 走单独分支，不受本次改动影响。"""
    got = E._rebuild_probs("blue_specialist", hist)
    assert got is not None and "blue" in got
    assert np.array_equal(got["blue"], np.asarray(M.blue_specialist(hist), dtype=float))


# ---------------------------------------------------------------------------
# 第二层根因：同一期数据被反复跑完整进化（幂等闸门）
# ---------------------------------------------------------------------------

def test_evolution_step_is_idempotent_per_data_fingerprint(seeded_db, monkeypatch):
    """同一份数据第二次调用必须**跳过**（不重跑 A/B 验证）。

    背景（实测）：`_scheduler_step` 每 1800s 调用一次，开奖日过 21:35 后每轮都进
    `_evolution_step`，而当天数据不再变化 → 同一份数据被反复跑完整进化
    （日志实测 2026-10-18 跑了三轮，每轮 19~22 分钟）。

    实现错误时如何失败：若幂等闸门缺失或指纹算错（例如只用期数、不含末期号），
    第二次调用会真的去跑 32 条假说的 A/B（本用例用 monkeypatch 计 verify 调用数，
    次数 > 0 即失败）。
    """
    from lottery import api_app, evolution

    seeded_db(700, seed=23)

    # 让「真跑」变得廉价且可计数：替换掉最贵的一步
    calls = {"n": 0}

    def fake_ab(hyp, draws, **kw):
        calls["n"] += 1
        return {"method": None, "hyp_id": hyp.get("id"), "n_issues": 1,
                "delta_red_hits": 0.0, "delta_blue_rate": 0.0}

    monkeypatch.setattr(evolution, "verify_hypothesis_ab", fake_ab)
    monkeypatch.setattr(evolution, "verify_discovery_hypothesis", fake_ab)

    state = api_app._evolution_state_path()
    if state.exists():
        state.unlink()

    r1 = api_app._evolution_step(use_llm=False)
    assert not r1.get("skipped"), f"首次不应跳过：{r1.get('skipped_reason')}"
    assert calls["n"] > 0, "首次真跑时必须发生过验证调用"

    calls["n"] = 0
    r2 = api_app._evolution_step(use_llm=False)
    assert r2.get("skipped") is True, "同一份数据第二次必须跳过"
    assert r2.get("fingerprint") == r1.get("fingerprint")
    assert calls["n"] == 0, f"跳过时不得再跑 A/B 验证，实际调用 {calls['n']} 次"


def test_evolution_step_force_bypasses_idempotency(seeded_db, monkeypatch):
    """``force=True``（手动触发 /api/evolution/run）必须**永不跳过**。"""
    from lottery import api_app, evolution

    seeded_db(700, seed=24)
    calls = {"n": 0}

    def fake_ab(hyp, draws, **kw):
        calls["n"] += 1
        return {"method": None, "hyp_id": hyp.get("id"), "n_issues": 1,
                "delta_red_hits": 0.0, "delta_blue_rate": 0.0}

    monkeypatch.setattr(evolution, "verify_hypothesis_ab", fake_ab)
    monkeypatch.setattr(evolution, "verify_discovery_hypothesis", fake_ab)

    state = api_app._evolution_state_path()
    if state.exists():
        state.unlink()

    api_app._evolution_step(use_llm=False)
    calls["n"] = 0
    r2 = api_app._evolution_step(use_llm=False, force=True)
    assert not r2.get("skipped"), "force=True 不得被幂等闸门跳过"
    assert calls["n"] > 0, "force=True 必须真的重新跑验证"


def test_evolution_fingerprint_changes_with_new_draw(seeded_db):
    """数据一更新（新一期），指纹必须变化 → 幂等闸门自动失效。

    实现错误时如何失败：若指纹只用 ``len(draws)`` 而两批数据期数相同
    （例如补录历史导致同期数但内容不同），闸门会误跳过。这里断言末期号参与指纹。
    """
    from lottery import api_app, db

    seeded_db(700, seed=25)
    d1 = db.load_draws()
    fp1 = f"{len(d1)}:{d1[-1].get('issue')}"

    # 追加一期 → 期数与末期号都应变化
    new = dict(d1[-1])
    new["issue"] = str(int(d1[-1]["issue"]) + 1)
    db.upsert_draws([new])
    d2 = db.load_draws()
    fp2 = f"{len(d2)}:{d2[-1].get('issue')}"

    assert fp2 != fp1, "新一期数据必须改变指纹（否则幂等闸门会误跳过）"
    assert str(d2[-1]["issue"]) in fp2, "指纹必须包含末期号，而不只是期数"
