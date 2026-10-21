"""P4：闲时挖掘调度器。

## 保的是什么

不是"能挖到规律"（真实数据上大概率一条都挖不到，那是诚实结果），
而是**三条工程约束**：

1. **默认关闭** —— 新机制先观察再开，不默认改变生产行为；
2. **可中断可续跑** —— 部署/OOM 杀进程不能丢进度、不能重头再来；
3. **让路** —— 挖掘跑满 CPU 会把 40 线程池吃光 → healthcheck 超时 → 容器被判不健康。

第 3 条是单机部署里最容易忽视的：一个"后台任务"把前台拖垮，比它不跑更糟。
"""
from __future__ import annotations

import json

import pytest

from lottery import config, idle_mining


@pytest.fixture()
def idle_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("LOTT_IDLE_MINING", "0")
    from lottery import db
    db.close()
    db.get_conn()
    yield tmp_path
    db.close()


def test_disabled_by_default(idle_env):
    """**默认必须关闭**：新机制不该默认改变生产行为。"""
    assert idle_mining.enabled() is False
    r = idle_mining.run_once()
    assert r["ok"] is False
    assert r["reason"] == "idle_mining_disabled"


def test_enabled_by_env(idle_env, monkeypatch):
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    assert idle_mining.enabled() is True


def test_state_roundtrip(idle_env):
    st = {"completed_dimensions": ["d1", "d2"], "verdicts": {"x": {"decision": "monitor"}}}
    idle_mining.save_state(st)
    loaded = idle_mining.load_state()
    assert loaded["completed_dimensions"] == ["d1", "d2"]
    assert loaded["verdicts"]["x"]["decision"] == "monitor"
    assert loaded["updated_at"] is not None


def test_corrupt_state_does_not_crash(idle_env):
    """坏断点文件必须当作"从头开始"，不能让整个调度卡死。"""
    p = idle_mining.state_path()
    p.write_text("{ this is not json", encoding="utf-8")
    st = idle_mining.load_state()
    assert st["cursors"] == {"single": 0, "cross": 0}
    assert st["space_version"] == idle_mining.SPACE_VERSION
    assert st["runs"] == 0


def test_save_state_is_atomic(idle_env):
    """先写 tmp 再 replace —— 中段被杀不会留下半写文件。"""
    idle_mining.save_state({"cursors": {"single": 3, "cross": 0}, "usable": {}})
    p = idle_mining.state_path()
    assert p.exists()
    json.loads(p.read_text(encoding="utf-8"))       # 必须是合法 JSON
    assert not p.with_suffix(".tmp").exists()


def test_resume_advances_cursor_not_repeat(idle_env, monkeypatch):
    """**续跑**：游标必须前移，同一索引不得重复判定。

    这条是 P4 生成式改造后「只挖没挖过的」的核心保证 ——
    旧实现靠 `completed_dimensions` 集合去重，新实现靠确定性游标。
    """
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    idle_mining.save_state({"space_version": idle_mining.SPACE_VERSION,
                            "cursors": {"single": 7, "cross": 0},
                            "usable": {}, "n_checked": 7})
    st = idle_mining.load_state()
    assert st["cursors"]["single"] == 7


def test_judge_candidate_delegates_to_eprocess(idle_env):
    """所有闲时挖掘的判定必须走同一入口（将来换统计方法只改一处）。"""
    import numpy as np
    z = list(np.random.default_rng(3).normal(0.2, 1, 200))
    v = idle_mining.judge_candidate(z)
    assert v["source"] == "idle_mining"
    for k in ("e_value", "decision", "n_obs", "alpha_spent"):
        assert k in v


def test_judge_candidate_underpowered_on_short_series(idle_env):
    v = idle_mining.judge_candidate([0.1] * 5)
    assert v["decision"] == "underpowered"


def test_status_reports_quota_fields(idle_env):
    """状态字段供 /api/health 暴露配额与进度（V13 P4 验收要求）。"""
    s = idle_mining.status()
    for k in ("enabled", "completed_dimensions", "runs", "total_candidates", "by_decision"):
        assert k in s
    assert s["enabled"] is False


def test_run_once_refuses_without_enough_data(idle_env, monkeypatch):
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    r = idle_mining.run_once(min_start=300)
    assert r["ok"] is False
    assert r["reason"] == "insufficient_data"


def test_idle_mining_evaluates_real_sequences(monkeypatch, seeded_db, tmp_path):
    """验证 P4-2：闲时挖掘真实计算 asof_scan 并产生 e-process 判定。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    seeded_db(400)
    
    res = idle_mining.run_once(budget_candidates=5, min_start=200)
    assert res["ok"] is True
    assert res["processed"] > 0
    
    state = idle_mining.load_state()
    # 判定记录现在按 decision 汇总计数（不再全量存 verdicts ——
    # 开放空间下候选数可达数万，全存会让断点膨胀到几十 MB）
    assert state["n_checked"] > 0
    assert sum(state["by_decision"].values()) == state["n_checked"]


# ---------- P4 接线：常驻循环真的会调用 run_once（V13 补齐项） ----------

def test_idle_mining_step_calls_run_once_when_enabled(idle_env, monkeypatch):
    """接线测的核心：`_idle_mining_step()` 必须真的走到 `run_once()`。

    这条防的是「模块写好了但没有任何调用点」—— 本项修复前的真实缺陷：
    `run_once` 全仓只有测试在调，调度器从不触发，health 永远 runs=0。
    """
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    called = {}
    monkeypatch.setattr(idle_mining, "run_once",
                        lambda **kw: called.update(kw) or {"ok": True, "processed": 0})
    r = api_app._idle_mining_step()
    assert r["ok"] is True
    assert called is not None            # 证明 run_once 被真正调用
    # 预算由 LOTT_IDLE_BUDGET 控制（轮要小、轮要多：断点细、CPU 平稳）
    assert called.get("budget_candidates") == api_app.IDLE_MINING_BUDGET


def test_idle_mining_step_noop_when_disabled(idle_env, monkeypatch):
    """关闭时**不得**调用 run_once —— 默认关闭的承诺要在接线层也成立。"""
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "0")
    hit = {"n": 0}

    def _boom(**kw):
        hit["n"] += 1
        raise AssertionError("关闭状态下不该调用 run_once")

    monkeypatch.setattr(idle_mining, "run_once", _boom)
    r = api_app._idle_mining_step()
    assert r["ok"] is False
    assert r["reason"] == "idle_mining_disabled"
    assert hit["n"] == 0


def test_idle_mining_loop_survives_exception(idle_env, monkeypatch):
    """后台任务抛异常不能打死线程，也不能拖垮前台（单机部署的头号风险）。"""
    from lottery import api_app
    monkeypatch.setenv("LOTT_IDLE_MINING", "1")
    monkeypatch.setattr(api_app, "IDLE_MINING_INTERVAL", 0)
    state = {"n": 0}

    def _flaky(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("模拟第一轮炸掉")
        raise SystemExit("第二轮主动退出循环")

    monkeypatch.setattr(idle_mining, "run_once", _flaky)
    with pytest.raises(SystemExit):
        api_app._idle_mining_loop()
    assert state["n"] == 2          # 第一轮异常后仍进入第二轮 => 线程没死


def test_idle_mining_independent_of_scheduler_switch(idle_env, monkeypatch):
    """两个开关语义独立：只开 idle 不开 scheduler 也必须能跑。"""
    from lottery import api_app
    assert hasattr(api_app, "_idle_mining_loop")
    # 接线代码读的是 LOTT_IDLE_MINING，不是 config.SCHEDULER_ENABLED
    src = __import__("inspect").getsource(api_app._idle_mining_loop)
    assert "LOTT_IDLE_MINING" not in src        # 门控在模块级，不在循环里


# ---------- CPU 限流（用户硬约束：整机 ≤60%） ----------

def test_cpu_limit_defaults_to_60pct():
    """用户约束：本 VPS 4 核，闲时挖掘整机占用不得超过 60%。"""
    assert idle_mining.CPU_LIMIT_PCT == pytest.approx(60.0)


def test_throttle_runs_full_speed_when_headroom_is_ample(idle_env, monkeypatch):
    """**整机余量充足时不额外休眠**（口径修正，2026-10-21）。

    `CPU_LIMIT_PCT` 是整机百分比（4 核 60% = 2.4 核），而单线程最多 1 核 = 25%，
    天然不可能突破上限。整机只用 10% 时不应为了「占空比」去睡 ——
    那是第一版的错误，它把单候选墙钟从 0.05s 拖到 0.29s。
    """
    slept = []
    monkeypatch.setattr(idle_mining.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(idle_mining, "_system_cpu_pct", lambda: 10.0)   # 余量 50 点
    monkeypatch.setattr(idle_mining, "_online_pressure", lambda: False)
    idle_mining._throttle(1.0)
    assert slept and slept[0] <= 0.05, f"余量充足却睡了 {slept[0]:.2f}s，过严"


def test_throttle_backs_off_when_headroom_below_one_core(idle_env, monkeypatch):
    """整机余量 **不足一个核** 时应让路（这才是限流的真实触发条件）。"""
    slept = []
    monkeypatch.setattr(idle_mining.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(idle_mining, "_cpu_count", lambda: 4)
    monkeypatch.setattr(idle_mining, "_system_cpu_pct", lambda: 50.0)   # 余量 10 < 25
    idle_mining._throttle(1.0)
    assert slept and slept[0] > 0.05, "余量不足一个核却没有让路"


def test_throttle_lets_more_work_sleep_more_under_pressure(idle_env, monkeypatch):
    """在余量不足时，本批用的 CPU 越多应休眠越久。"""
    slept = []
    monkeypatch.setattr(idle_mining.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(idle_mining, "_cpu_count", lambda: 4)
    monkeypatch.setattr(idle_mining, "_system_cpu_pct", lambda: 52.0)   # 余量 8 < 25
    idle_mining._throttle(0.2)
    idle_mining._throttle(1.0)
    assert len(slept) == 2
    assert slept[1] > slept[0], "CPU 用得多时睡得应更久"


def test_throttle_backs_off_when_system_already_at_limit(idle_env, monkeypatch):
    """整机已达/超 60% 时，本批应让路（长休眠），绝不硬挤。"""
    slept = []
    monkeypatch.setattr(idle_mining.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(idle_mining, "_system_cpu_pct", lambda: 85.0)  # 已超限额
    idle_mining._throttle(0.3)
    assert slept and slept[0] >= idle_mining.CPU_SAMPLE_WINDOW, "超限额时应当让路"


def test_throttle_never_sleeps_absurdly_long(idle_env, monkeypatch):
    """休眠有上限：否则 3 天观察窗口会被无意义的长眠吃掉。"""
    slept = []
    monkeypatch.setattr(idle_mining.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(idle_mining, "_system_cpu_pct", lambda: 1.0)
    monkeypatch.setattr(idle_mining, "_online_pressure", lambda: True)
    idle_mining._throttle(100.0)
    assert slept[0] <= 30.0


# ---------- 分层 e-BH（D2） ----------

def test_ebh_stays_local_to_its_layer(idle_env, monkeypatch):
    """交叉层用**单组** m，不是全局 72 656 —— 否则阈值 1 447 000 等于放弃该层。

    ## V14-A1 起 m 取「该族对 × 该模式的真实档位数」，不再取常数 135

    旧断言依赖 ``m≈135 ⇒ 阈值 2700``，那正是被团队判定为缺陷的行为：
    135 是分布的**均值**，实测族对档位数中位只有 35、78% 的族对低于 135，
    把均值当常数会让低于均值的组阈值被凭空抬 135 倍。

    现在用 `hot×streak`（真实档位数 132 ⇒ 阈值 2640），让 e=5000 能在
    真实 m 下通过，同时验证阈值仍远小于全局 72656/α。
    """
    cands = {
        'cross|0|hot:{"window":10,"k":5}|streak:{"n":3}': {"e_value": 5000.0},
        'cross|0|hot:{"window":10,"k":6}|streak:{"n":3}': {"e_value": 3000.0},
        'single|hot|{"window":10,"k":5}': {"e_value": 800.0},
    }
    got = idle_mining._recompute_usable(cands, n_checked=72656)
    assert any(k.startswith('cross|0|hot') for k in got), "交叉层在本层 m 下应当有候选通过"
    for k, v in got.items():
        assert v.get("ebh_threshold_k1", 1) < 20000, "阈值不应被全局 m 抬死"


def test_group_m_equals_real_family_pair_size(idle_env):
    """**V14-A1 核心**：m 必须等于该族对的真实档位数，不能是任何字面量常数。

    实测基线（`candidate_space`）：族对档位数 min/中位/max = 1/35/2304，
    最小与最大差 2304 倍，**不存在能代表所有组的单一常数**。
    """
    pairs = idle_mining._family_pairs()
    assert len(pairs) == 268 * 2, (
        f"族对+模式组数应为 268×2=536，实际 {len(pairs)} —— 分组键可能又塌陷了")

    sizes = sorted(pairs.values())
    assert sizes[0] == 1, "最小族对档位数应为 1（如 repeat×repeat2）"
    assert max(sizes) > 1000, "最大族对档位数应 >1000（如 hot×sum_range 类）"
    assert len(set(sizes)) > 10, "组规模必须有显著离散度，否则常数近似才有理"

    for gkey, real in list(pairs.items())[:20]:
        assert idle_mining._group_checked(gkey, 18200, 1) == real, (
            f"{gkey}: _group_checked 返回 {idle_mining._group_checked(gkey, 18200, 1)}，"
            f"真实档位数 {real}")


def test_group_key_includes_both_families(idle_env):
    """分组键必须含「两个族名 + 模式」，否则交叉层会塌陷成 2 组。

    旧实现 `key = "cross|" + parts[1]` 只取 mode，实测整个交叉层只有
    `cross|0` 与 `cross|1` 两组（11079 + 26793 个候选），
    与设计意图「268 族对 × 2 模式」相差 268 倍。
    """
    a = 'cross|0|hot:{"window":10,"k":5}|cold:{"window":10,"k":5}'
    b = 'cross|0|hot:{"window":10,"k":6}|cold:{"window":10,"k":5}'
    c = 'cross|0|hot:{"window":10,"k":5}|sum_range:{"lo":45}'
    d = 'cross|1|hot:{"window":10,"k":5}|cold:{"window":10,"k":5}'

    ka, kb = idle_mining._group_key_of(a), idle_mining._group_key_of(b)
    kc, kd = idle_mining._group_key_of(c), idle_mining._group_key_of(d)

    assert ka == kb, "同族对的不同参数档位应落在同一组"
    assert ka != kc, "不同族对必须分组"
    assert ka != kd, "不同模式必须分组"
    assert ka.count("|") >= 3, f"组键 {ka} 缺族对信息（可能又退化成 cross|<mode>）"


def test_repeat_times_repeat2_gets_tiny_m(idle_env):
    """`repeat×repeat2` 的真实档位数极小，旧实现把它放大 135 倍。

    这是「把均值当常数」最典型的受害者：真实 m 只有 1，阈值应为 1/α = 20，
    旧实现给出 135/α = 2700 —— 任何真实信号都过不去。
    """
    pairs = idle_mining._family_pairs()
    key = 'cross|0|repeat|repeat2'
    if key not in pairs:
        pytest.skip("该族对不在当前候选空间中")
    real = pairs[key]
    assert real < 135, f"repeat×repeat2 真实档位数 {real}，应远小于常数 135"
    assert idle_mining._group_checked(key, 18200, 1) == real


def test_ebh_zero_when_nothing_exceeds_local_threshold(idle_env):
    """弱证据在本层阈值下仍然不通过（不报假规律的底线）。"""
    cands = {'single|hot|{"window":10,"k":5}': {"e_value": 25.0}}
    got = idle_mining._recompute_usable(cands, n_checked=306)
    # 单因子层 m≈306 ⇒ 阈值 6120；e=25 差太远
    assert got == {}
