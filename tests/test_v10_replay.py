"""V10-05 离线回放分表（``replay_results``）的测试。

覆盖五组必须证明的事（``docs/V10_IMPROVEMENT_PLAN.md`` §2.1.5 / §3.6）：

1. **migration #3**：冻结 DDL 逐字落地、幂等、与既有迁移共存、批次事务仍工作、
   **主键含 mode**（``replay`` 与 ``research_arm`` 不得互相覆盖）。
2. **严格 as-of**：第 t 期只用 ``draws[:t]``——改未来 → 结果一字不变；改历史 →
   结果必变；阈值/热度只用**滚动窗口**（超出窗口的更早历史不影响）；
   并有一条**反向用例**：构造"只有偷看未来才会出现的好结果"，断言实现没有出现。
3. **自证统计性质**：注入偏置必须被检出；纯随机序列不得系统性误报
   （固定序列不显著 + 跨 seed 假阳性率与 α 同量级）。
4. **物理分表**：回放**不触碰** ``eval_details``，两套样本只能并排、禁止相加。
5. **口径细节**：``channels_fp`` 落库且来自**显式**通道、幂等重跑不增行、
   ``ml``/``llm`` 不可回放被诚实跳过、MDE 复用 ``rigor.min_sample_size``。

约定：本目录 ``tests/conftest.py`` 已 autouse 隔离 ``config`` 的库路径与数据目录；
本文件**不**裸赋值 ``config.DB_PATH``，需要数据时用 ``seeded_db`` / ``make_draws``。
"""
from __future__ import annotations

import random
import sqlite3

import pytest

from lottery import db, migrations, rigor

# --------------------------------------------------------------------------
# 测试数据构造
# --------------------------------------------------------------------------

START = 40          # 回放起始下标（前期数据不足以建分布）
N_TICKETS = 2


def _draws(n: int, seed: int = 42, bias: float = 0.0) -> list:
    """合成开奖序列（升序，结构同 ``db.load_draws()``）。

    ``bias>0`` 时号码 1 以该概率被"系统性加权"进红球（注入偏置场景）。
    """
    rng = random.Random(seed)
    out = []
    for i in range(n):
        if bias and rng.random() < bias:
            reds = sorted([1] + rng.sample(range(2, 34), 5))
        else:
            reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"{2020000 + i}",
            "date": "2020-01-01",
            "reds": reds,
            "order": list(reds),
            "blue": rng.randint(1, 16),
        })
    return out


def _row_key(rows, tmax=None):
    """回放行的可比摘要（只取落库口径的字段，忽略 created_at）。"""
    return [
        (r["method"], r["issue"], r["n_tickets"], r["red_hits"],
         r["best_red_hit"], r["blue_hit"], r["prize"])
        for r in rows if (tmax is None or r["t"] <= tmax)
    ]


def _fresh_db(tmp_path, name="replay_mig.db"):
    conn = sqlite3.connect(tmp_path / name)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ==========================================================================
# 1. migration #3（冻结 DDL / 幂等 / 共存 / 批次事务 / 主键含 mode）
# ==========================================================================

def test_migration_3_creates_frozen_ddl(tmp_path):
    """migration #3 编号 = ``MIGRATIONS[-1][0]``，且 DDL 与 §2.1.5 冻结文本一致。"""
    from lottery import replay as R

    assert migrations.MIGRATIONS[-1][0] == 3, "V10 期间编号冻结为第 3 号"
    conn = _fresh_db(tmp_path)
    assert migrations.apply(conn) == 3

    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(replay_results)")}
    assert set(cols) == set(R.COLUMNS), "列集合必须与冻结 DDL 完全一致"
    # issue 必须是 TEXT（与 draws.issue / eval_details.issue 一致）
    assert str(cols["issue"]["type"]).upper() == "TEXT"
    assert cols["mode"]["dflt_value"] in ("'replay'", '"replay"')
    assert cols["n_tickets"]["notnull"] == 1 and cols["created_at"]["notnull"] == 1

    # 主键必须是 (method, mode, issue)——按 DDL 顺序
    pk = [r["name"] for r in sorted(
        (r for r in conn.execute("PRAGMA table_info(replay_results)") if r["pk"]),
        key=lambda r: r["pk"])]
    assert pk == ["method", "mode", "issue"], "主键必须含 mode（S2 B4）"
    # 索引也在
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='replay_results'")}
    assert "idx_replay_issue" in idx
    conn.close()


def test_migration_3_idempotent_and_coexists_with_earlier_migrations(tmp_path):
    """重复 apply 幂等；与 #1/#2 共存（schema_version 逐条记录，不重写）。"""
    conn = _fresh_db(tmp_path)
    latest = migrations.MIGRATIONS[-1][0]
    assert migrations.apply(conn) == latest
    assert migrations.apply(conn) == latest
    assert migrations.apply(conn) == latest
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == [v for v, _, _ in migrations.MIGRATIONS]
    # #1/#2 的产物也还在（迁移没互相踩）
    names = _tables(conn)
    assert {"reconcile_runs", "schema_version", "replay_results"} <= names
    conn.close()


def test_migration_3_rolls_back_with_batch_transaction(tmp_path, monkeypatch):
    """批次事务仍然工作：后续迁移失败 → ``replay_results`` 的 DDL 一并回滚。"""
    conn = _fresh_db(tmp_path)

    def _boom(c):
        raise RuntimeError("injected failure after replay_results")

    real = migrations._ensure_replay_results
    monkeypatch.setattr(migrations, "MIGRATIONS", [
        (1, "reconcile audit table", migrations._ensure_reconcile_runs),
        (2, "legacy provenance columns", migrations._ensure_legacy_columns),
        (3, "v10 replay results", real),
        (4, "injected fail", _boom),
    ])
    with pytest.raises(RuntimeError, match="injected failure"):
        migrations.apply(conn, backup=False)
    # #3 的建表被回滚：表不存在，版本不前进
    assert "replay_results" not in _tables(conn)
    assert migrations.current_version(conn) == 0
    conn.close()


def test_replay_results_primary_key_includes_mode(tmp_path):
    """``INSERT OR REPLACE``：同一 (method, issue) 的 replay 与 research_arm 互不覆盖。"""
    conn = _fresh_db(tmp_path)
    migrations.apply(conn)
    sql = ("INSERT OR REPLACE INTO replay_results "
           "(method, mode, issue, n_tickets, red_hits, best_red_hit, blue_hit, prize, "
           " channels_fp, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(sql, ("stat:freq", "replay", "2026001", 5, 6, 3, 1, 5, "fpA", 1.0))
    conn.execute(sql, ("stat:freq", "research_arm", "2026001", 5, 9, 4, 2, 10, "fpB", 2.0))
    rows = conn.execute("SELECT mode, red_hits FROM replay_results ORDER BY mode").fetchall()
    assert [(r["mode"], r["red_hits"]) for r in rows] == [("replay", 6), ("research_arm", 9)]

    # 再写 replay（另一份通道）→ 只覆盖 replay 那行，research_arm 原样保留
    conn.execute(sql, ("stat:freq", "replay", "2026001", 5, 7, 3, 1, 5, "fpC", 3.0))
    got = dict(conn.execute("SELECT mode, red_hits FROM replay_results").fetchall())
    assert got == {"replay": 7, "research_arm": 9}, "主键缺 mode 时这里是 1 行、且 research_arm 被毁"
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM replay_results").fetchone()[0] == 2
    conn.close()


# ==========================================================================
# 2. 严格 as-of
# ==========================================================================

def test_replay_ignores_future_draws():
    """改第 t0 期之后的数据 → t0 及之前的所有回放行**一字不变**。"""
    from lottery import replay as R

    draws = _draws(100, seed=42)
    t0 = 60
    base = R.replay_rows(draws, ["stat:freq", "uniform"], n_tickets=N_TICKETS,
                         start=START, seed=7)

    tampered = [dict(d) for d in draws]
    for i in range(t0 + 1, len(tampered)):        # 只改 t0 的"未来"
        tampered[i] = {**tampered[i], "reds": [1, 2, 3, 4, 5, 6], "blue": 1}
    after = R.replay_rows(tampered, ["stat:freq", "uniform"], n_tickets=N_TICKETS,
                          start=START, seed=7)

    assert _row_key(base, t0) == _row_key(after, t0), "回放偷看了未来数据"
    # 反向确认"改未来"确实生效（否则上面的相等可能来自测试没改到东西）
    assert _row_key(base) != _row_key(after)


def test_replay_uses_past_history():
    """改历史 → 结果必变（证明回放不是"什么都没看"的空壳）。"""
    from lottery import replay as R

    draws = _draws(100, seed=42)
    base = R.replay_rows(draws, ["stat:freq"], n_tickets=N_TICKETS, start=START, seed=7)

    tampered = [dict(d) for d in draws]
    rng = random.Random(999)
    for i in range(0, START - 1):
        reds = sorted(rng.sample(range(1, 34), 6))
        tampered[i] = {**tampered[i], "reds": reds, "order": list(reds)}
    after = R.replay_rows(tampered, ["stat:freq"], n_tickets=N_TICKETS, start=START, seed=7)
    assert _row_key(base) != _row_key(after)


def test_as_of_threshold_only_sees_rolling_window():
    """阈值/热度只用**滚动窗口**：超出窗口的更早历史不影响，窗口内历史必影响。

    ``uniform`` 出号不依赖概率分布、只依赖 ``constraint_ctx``（和值分位、分区、
    奇偶区间——即"阈值/分位"本体），因此它把"窗口"这件事暴露得最干净。
    """
    from lottery import replay as R

    window = 20
    draws = _draws(100, seed=42)
    base = R.replay_rows(draws, ["uniform"], n_tickets=N_TICKETS, start=START,
                         seed=7, window=window)

    # (a) 改"窗口外的更早历史"（index < START-window）→ 不变
    far = [dict(d) for d in draws]
    rng = random.Random(999)
    for i in range(0, START - window):
        reds = sorted(rng.sample(range(1, 34), 6))
        far[i] = {**far[i], "reds": reds, "order": list(reds), "blue": (i % 16) + 1}
    assert _row_key(base) == _row_key(
        R.replay_rows(far, ["uniform"], n_tickets=N_TICKETS, start=START,
                      seed=7, window=window)), "窗口外的历史不该影响阈值（否则退化成全样本）"

    # (b) 改"窗口内的历史"（index ∈ [START-window, START)）→ 必变
    near = [dict(d) for d in draws]
    for i in range(START - window, START):
        near[i] = {**near[i], "reds": [1, 2, 3, 4, 5, 6], "blue": 1}
    assert _row_key(base) != _row_key(
        R.replay_rows(near, ["uniform"], n_tickets=N_TICKETS, start=START,
                      seed=7, window=window)), "窗口内历史必须参与阈值计算"

    # (c) 纯函数的窗口语义：热度只看最近 window 期
    hist = draws[:START]
    assert R.as_of_hot_reds(hist, window=window) == R.as_of_hot_reds(hist[-window:], window=window)
    assert R.as_of_hot_reds(hist, window=window) != R.as_of_hot_reds(hist, window=len(hist))
    # 每行的 as_of_hot 必须等于「只用 draws[:t] 的最近 window 期」算出的值
    rows = R.replay_rows(draws, ["uniform"], n_tickets=1, start=START, seed=7, window=window)
    for r in rows:
        assert r["as_of_hot"] == R.as_of_hot_reds(draws[:r["t"]], window=window)


def test_full_sample_threshold_would_show_fake_gain():
    """**反向用例**：只有偷看未来才会出现的好结果，实现不得出现。

    构造：前 80 期**从不**开出 33；从第 81 期起**每期都**开出 33。
      * 全样本热度（含未来）会立刻把 33 选进 hot set —— 用它给早期期次出号，
        早期命中率会平白抬高（"偷看未来的免费午餐"）；
      * 严格 as-of 的实现在第 t<=80 期根本不知道 33 会变热，早期命中率必须贴
        近理论基线，hot set 也不得包含 33。
    """
    from lottery import replay as R

    n = 160
    rng = random.Random(5)
    draws = []
    for i in range(n):
        if i < 80:
            reds = sorted(rng.sample(range(1, 33), 6))          # 1..32，33 不出现
        else:
            reds = sorted([33] + rng.sample(range(1, 33), 5))   # 33 每期都出现
        draws.append({"issue": f"{2020000 + i}", "date": "2020-01-01",
                      "reds": reds, "order": list(reds), "blue": rng.randint(1, 16)})

    # 全样本 hot 含 33（这正是"偷看未来"会拿到的阈值）
    assert 33 in R.as_of_hot_reds(draws)
    # as-of @ t=80 的 hot 不含 33
    assert 33 not in R.as_of_hot_reds(draws[:80])

    res = R.offline_replay(draws, methods=["stat:freq"], n_tickets=3,
                           start=START, persist=False)
    early = [r for r in res["rows"] if r["t"] < 80]
    late = [r for r in res["rows"] if r["t"] >= 90]
    assert early and late
    # 早期 hot set 一律不含 33（偷看实现会含）
    assert all(33 not in r["as_of_hot"] for r in early)
    early_mean = sum(r["red_hits"] for r in early) / sum(r["n_tickets"] for r in early)
    late_mean = sum(r["red_hits"] for r in late) / sum(r["n_tickets"] for r in late)
    # 早期命中率贴近理论基线（1.0909），没有"免费午餐"；晚期确实受益（作为对照，
    # 证明这条序列本身是能抬高命中率的，因此早期持平是真的没偷看）
    assert early_mean < rigor.BASELINE_MEAN_HITS + 0.15, (
        f"早期命中率 {early_mean:.3f} 明显偏高，疑似用了未来数据算阈值")
    assert late_mean > early_mean + 0.15, (
        "对照失效：晚期（33 真变热之后）应当明显高于早期，否则该场景不构成反向用例")


# ==========================================================================
# 3. 自证统计性质：注入偏置必检出 / 真随机不误报
# ==========================================================================

def test_injected_bias_is_detected():
    """号码 1 被系统性加权（60%）→ 频率法回放必须报告显著优于基线。"""
    from lottery import replay as R

    draws = _draws(160, seed=11, bias=0.6)
    res = R.offline_replay(draws, methods=["stat:freq"], n_tickets=3,
                           start=START, persist=True)
    block = res["per_method"]["stat:freq"]
    assert block["red_hits_mean"] > rigor.BASELINE_MEAN_HITS, "偏置没被利用"
    sig = block["significance"]
    assert sig["n"] == block["n_issues"] == 120
    assert sig["verdict"] == "significant-after-correction", sig.get("message_zh")
    assert sig["p_value"] < 0.001
    # 落库也记下了同样的样本量
    rows = R.load_replay_rows(mode=R.MODE_REPLAY, method="stat:freq")
    assert len(rows) == 120
    assert sum(int(r["n_tickets"]) for r in rows) == 120 * 3


def test_pure_random_is_not_significant():
    """纯随机序列（固定 seed）→ 各方法都不得报告显著（不得误报）。"""
    from lottery import replay as R

    draws = _draws(160, seed=11)          # bias=0：真随机
    res = R.offline_replay(
        draws, methods=["stat:freq", "stat:omission", "stat:markov", "uniform"],
        n_tickets=3, start=START, persist=False)
    for method, block in res["per_method"].items():
        sig = block["significance"]
        assert sig["survives_correction"] is False, (
            f"{method} 在纯随机序列上被判显著（p={sig['p_value']:.4f}）= 误报")
        assert sig["verdict"] in ("not-significant", "insufficient-data"), method
    # 命中率应贴近理论基线（不是被某种偏置抬高的）
    for method, block in res["per_method"].items():
        assert abs(block["red_hits_mean"] - rigor.BASELINE_MEAN_HITS) < 0.25, method


def test_random_false_positive_rate_is_near_alpha():
    """跨 16 条独立随机序列：假阳性个数必须与 α=0.05 同量级（不是系统性误报）。

    单条序列上偶发的 p<0.05 是**统计学正常**（α 的定义就是"5% 概率误报一次"），
    这里用固定 seed 集合把"没有系统性偏置"钉成可复现断言。
    """
    from lottery import replay as R

    alpha = 0.05
    n_series = 16
    hits = 0
    for seed in range(100, 100 + n_series):
        res = R.offline_replay(_draws(120, seed=seed), methods=["stat:freq"],
                               n_tickets=2, start=START, persist=False)
        if res["per_method"]["stat:freq"]["significance"]["p_value"] < alpha:
            hits += 1
    # 期望 0.8 个（16 × 0.05）；>=4 个才说明存在系统性偏置（本机实测 1/16）
    assert hits <= 3, f"{n_series} 条随机序列出现 {hits} 次显著，假阳性率异常偏高"


# ==========================================================================
# 4. 物理分表：回放不触碰 eval_details；两套样本并排、禁止相加
# ==========================================================================

def test_replay_does_not_touch_eval_details(seeded_db):
    """回放写 ``replay_results``，**不得**动 ``eval_details``（反之亦然）。"""
    from lottery import evaluate as EV
    from lottery import replay as R

    seeded_db(160, seed=11)
    draws = db.load_draws()
    # 先造一批真实交战样本（seeded_db 的期号形如 2013001）
    for issue, method, ticket in (
        ("2013001", "stat:freq", {"reds": [1, 2, 3, 4, 5, 6], "blue": 5}),
        ("2013002", "stat:freq", {"reds": [2, 3, 4, 5, 6, 7], "blue": 9}),
    ):
        draw = next(d for d in draws if d["issue"] == issue)
        db.save_eval_details(issue, method, [ticket], draw)

    conn = db.get_conn()
    live_before = conn.execute("SELECT COUNT(*) FROM eval_details").fetchone()[0]
    breakdown_before = EV.method_breakdown()

    res = R.offline_replay(methods=["stat:freq", "uniform"], n_tickets=N_TICKETS,
                           start=START, persist=True)
    assert res["n_written"] > 0

    assert conn.execute("SELECT COUNT(*) FROM eval_details").fetchone()[0] == live_before
    assert EV.method_breakdown() == breakdown_before, "回放污染了真实交战报表"
    # replay_results 自己确实写进去了
    assert conn.execute("SELECT COUNT(*) FROM replay_results").fetchone()[0] == res["n_written"]


def test_samples_side_by_side_separates_and_forbids_combining(seeded_db):
    """两套样本只能并排（各自样本量 + 各自 MDE），返回体不得含任何"合计"字段。"""
    from lottery import replay as R

    seeded_db(160, seed=11)
    draws = db.load_draws()
    db.save_eval_details("2013001", "stat:freq",
                         [{"reds": [1, 2, 3, 4, 5, 6], "blue": 1}],
                         next(d for d in draws if d["issue"] == "2013001"))
    R.offline_replay(methods=["stat:freq"], n_tickets=N_TICKETS, start=START, persist=True)

    side = R.samples_side_by_side()
    assert side["mixable"] is False
    assert side["live"]["source"] == R.SOURCE_LIVE
    assert side["replay"]["source"] == R.SOURCE_REPLAY
    assert side["live"]["n_issues"] == 1
    assert side["replay"]["n_issues"] == 160 - START
    # 各自都有样本量与 MDE，且真实样本的 MDE 远大于回放样本（这正是立项理由）
    for block in (side["live"], side["replay"]):
        assert block["n_issues"] > 0 and block["n_tickets"] > 0
        assert block["mde"] is not None and block["mde"] > 0
    assert side["live"]["mde"] > side["replay"]["mde"]
    # 禁止相加：顶层与两块都不得出现跨来源字段
    forbidden = {"combined", "pooled", "total", "merged", "sum"}
    assert not (forbidden & set(side))
    assert not (forbidden & set(side["live"]))
    assert not (forbidden & set(side["replay"]))
    assert "禁止相加" in side["note_zh"]


def test_evaluate_exposes_side_by_side_entry(seeded_db):
    """报表侧的稳定入口 ``evaluate.samples_side_by_side()`` 与 replay 同源（懒导入，无循环）。"""
    from lottery import evaluate as EV
    from lottery import replay as R

    seeded_db(120, seed=11)
    R.offline_replay(methods=["stat:freq"], n_tickets=N_TICKETS, start=START, persist=True)
    side = EV.samples_side_by_side()
    assert side == R.samples_side_by_side()
    assert side["replay"]["n_issues"] == 120 - START
    # 既有签名不受影响：method_breakdown 仍可独立调用
    assert "families" in EV.method_breakdown()


# ==========================================================================
# 5. 口径细节：幂等 / 显式通道 / 不可回放方法 / 非法 mode / MDE
# ==========================================================================

def test_offline_replay_is_idempotent(seeded_db):
    """同一 (method, mode, issue) 重跑不产生重复行（INSERT OR REPLACE）。"""
    from lottery import replay as R

    seeded_db(120, seed=11)
    r1 = R.offline_replay(methods=["stat:freq"], n_tickets=N_TICKETS, start=START, persist=True)
    conn = db.get_conn()
    c1 = conn.execute("SELECT COUNT(*) FROM replay_results").fetchone()[0]
    r2 = R.offline_replay(methods=["stat:freq"], n_tickets=N_TICKETS, start=START, persist=True)
    c2 = conn.execute("SELECT COUNT(*) FROM replay_results").fetchone()[0]
    assert r1["n_written"] == r2["n_written"] == c1 == c2 == 120 - START
    # 两种 mode 也各占各的主键，互不覆盖
    R.offline_replay(methods=["stat:freq"], n_tickets=N_TICKETS, start=START,
                     persist=True, mode=R.MODE_RESEARCH_ARM)
    by_mode = dict(conn.execute(
        "SELECT mode, COUNT(*) FROM replay_results GROUP BY mode").fetchall())
    assert by_mode == {R.MODE_REPLAY: 120 - START, R.MODE_RESEARCH_ARM: 120 - START}


def test_channels_fp_is_explicit_and_written(monkeypatch):
    """``channels_fp`` 落库；通道来自**显式** ``resolve_channels(force_production=True)``。"""
    from lottery import methods as METH
    from lottery import replay as R

    draws = _draws(80, seed=11)
    # 默认（无 methods_config.json、无 env）走 DEFAULT_SPEC_RAW = stat,ml,uniform
    ch = R.resolve_replay_channels()
    assert ch["mode"] == "production", "回放必须强制 production 模式解析"
    assert ch["fingerprint"] == METH.resolve_channels(force_production=True)["fingerprint"]
    # 通道决定默认回放哪些方法：blend 不在默认 allow-list 里，stat/ml/uniform 在
    assert "blend" not in R.channels_methods(ch)
    assert {"stat:freq", "uniform"} <= set(R.channels_methods(ch))

    res = R.offline_replay(draws, n_tickets=1, start=70, persist=True)
    assert res["channels_fp"] == ch["fingerprint"]
    rows = R.load_replay_rows(mode=R.MODE_REPLAY)
    assert rows and all(r["channels_fp"] == ch["fingerprint"] for r in rows)

    # 换一份显式通道（env 层）→ fingerprint 变、可回放方法集变
    monkeypatch.setenv("LOTT_METHODS", "all")
    ch2 = R.resolve_replay_channels()
    assert ch2["fingerprint"] != ch["fingerprint"]
    assert "blend" in R.channels_methods(ch2)
    res2 = R.offline_replay(draws, methods=["blend"], n_tickets=1, start=78,
                            channels=ch2, persist=True)
    assert res2["channels_fp"] == ch2["fingerprint"]
    got = {r["channels_fp"] for r in R.load_replay_rows(mode=R.MODE_REPLAY, method="blend")}
    assert got == {ch2["fingerprint"]}, "channels_fp 必须记录产出该批回放的通道指纹"


def test_research_mode_config_does_not_leak_into_replay(tmp_path, monkeypatch):
    """``methods_config.json`` 写着 research 模式时，回放仍按 production 解析。"""
    import json
    from lottery import config
    from lottery import replay as R

    config.METHODS_CONFIG_FILE.write_text(
        json.dumps({"raw": "stat", "mode": "research"}), encoding="utf-8")
    ch = R.resolve_replay_channels()
    assert ch["mode"] == "production"
    assert ch["families"]["stat"] is True and ch["families"]["uniform"] is False
    assert R.channels_methods(ch) == [m for m in R.REPLAYABLE_METHODS if m.startswith("stat:")]


def test_incomplete_channels_are_rejected_fail_closed():
    """显式通道缺 ``fingerprint`` / ``spec_raw`` → 直接报错，不得回落到环境变量默认。"""
    from lottery import replay as R

    draws = _draws(60, seed=11)
    with pytest.raises(ValueError, match="fingerprint"):
        R.offline_replay(draws, methods=["stat:freq"], start=50, persist=False, channels={})
    with pytest.raises(ValueError, match="spec_raw"):
        R.offline_replay(draws, methods=["stat:freq"], start=50, persist=False,
                         channels={"fingerprint": "abc", "families": {"stat": True}})
    with pytest.raises(ValueError, match="spec_raw"):
        R.replay_rows(draws, ["stat:freq"], start=50,
                      channels={"fingerprint": "abc", "families": {"stat": True}})


def test_non_replayable_methods_are_skipped_honestly():
    """``ml`` / ``llm`` 不可回放：进 ``skipped`` 并给原因，不产生任何行。"""
    from lottery import replay as R

    draws = _draws(80, seed=11)
    res = R.offline_replay(draws, methods=["stat:freq", "ml", "llm:deepseek-v4"],
                           n_tickets=1, start=70, persist=False)
    assert res["methods"] == ["stat:freq"]
    assert set(res["skipped"]) == {"ml", "llm:deepseek-v4"}
    assert "重训" in res["skipped"]["ml"] or "不可回放" in res["skipped"]["ml"]
    assert all(r["method"] == "stat:freq" for r in res["rows"])


def test_invalid_mode_is_rejected():
    """mode 只允许 replay / research_arm（主键 semantics 以外一律报错）。"""
    from lottery import replay as R

    draws = _draws(60, seed=11)
    with pytest.raises(ValueError, match="replay mode"):
        R.offline_replay(draws, methods=["stat:freq"], start=50, persist=False, mode="live")
    with pytest.raises(ValueError):
        R.load_replay_rows(mode="whatever")
    # 方案 §3.6 的接口名 run_replay 与主实现同一函数（不是第二份实现）
    assert R.run_replay is R.offline_replay


def test_detectable_mde_inverts_existing_min_sample_size():
    """MDE 反解 ``rigor.min_sample_size``（不新写功效函数），且与既有实现一致。"""
    from lottery import replay as R

    for n in (5, 30, 120, 600):
        mde = R.detectable_mde(n)
        assert mde is not None and mde > 0
        # 不变式：min_sample_size(mde) <= n < min_sample_size(mde*(1-eps))
        assert rigor.min_sample_size(mde, rigor.BASELINE_SD_HITS) <= n
        assert rigor.min_sample_size(mde * (1 - 1e-6), rigor.BASELINE_SD_HITS) > n
        # 与 rigor 既有 MDE 实现（同一公式）在 1e-9 内一致
        assert mde == pytest.approx(
            rigor._min_detectable_effect(n)["delta_hits"], rel=1e-9, abs=1e-12)
    assert R.detectable_mde(0) is None
    # 单调性：样本越多，可检出的效应越小
    assert R.detectable_mde(100) > R.detectable_mde(1000)
