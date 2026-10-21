"""V14-B1：`draws` 的六条数据轴必须真的被读出来。

## 为什么这个测试存在

在此之前 `_row_to_draw()` 只返回 `issue/date/reds/blue/order/sales/pool/source`，
**`p1`、`p1_amt`、`p2..p6` 被静默丢弃**。这些字段在库里有真实数据，是唯一能反映
「人群怎么选号」的观测量 —— 而号码本身已被证明不可预测（红球 χ² p=0.065，
边际熵 5.0429 / 上限 5.0444）。

丢弃它们是**静默**的：没有异常、没有日志、1292 个既有测试全部照常通过。
这正是本项目反复出现的那类缺陷 —— 主路径对，数据层悄悄少一块。

## 两类用例

* **结构用例**（始终跑）：用合成行验证 `_row_to_draw` 的键集合与鲁棒性，
  不依赖真实库，因此不会被 `conftest` 的库隔离影响；
* **真值用例**（有真实库才跑）：在真实 3517 期上断言 p1/p1_amt 真的流过来了。
  用 `_require_real_db()` 在**执行期**判定（与 `test_v12_shape_lock.py` 同惯例）——
  不能在模块导入期缓存成 `skipif`，那会与 conftest 的隔离时序脱钩。
"""
from __future__ import annotations

import sqlite3

import pytest

from lottery import db


def _has_real_db() -> bool:
    """执行期判定：当前是否连着真实库（>3000 期）。"""
    try:
        n = db.count_draws()
        return bool(n and n > 3000)
    except Exception:  # noqa: BLE001
        return False


def _require_real_db(why: str = "需要真实库（>3000 期）；conftest 默认隔离到临时库"):
    if not _has_real_db():
        pytest.skip(why)


def _fake_row(with_prize: bool = True):
    """构造一个与 `draws` 同构的 sqlite3.Row。"""
    cols = ["issue", "date"] + [f"r{i}" for i in range(1, 7)] + ["blue"] \
        + [f"o{i}" for i in range(1, 7)] + ["sales", "pool"]
    if with_prize:
        for lv in range(1, 7):
            cols += [f"p{lv}", f"p{lv}_amt"]
    cols += ["source"]
    vals = ["2026999", "2026-10-21"] + [1, 2, 3, 4, 5, 6] + [7] \
        + [7, 1, 2, 3, 4, 5] + [123, 456]
    if with_prize:
        vals += [9, 7232345, 73, 100000, 500, 3000, 20000, 200, 100000, 10, 200000, 5]
    vals += ["test"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE t ({', '.join(cols)})")
    conn.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(cols))})", vals)
    row = conn.execute("SELECT * FROM t").fetchone()
    conn.close()
    return row


# ---------- 1. 结构用例（不依赖真实库，始终跑） ----------

def test_row_to_draw_carries_all_prize_keys():
    """`_row_to_draw` 必须带出 p1..p6 与 p1_amt..p6_amt（12 个键）。"""
    out = db._row_to_draw(_fake_row(with_prize=True))
    for lv in range(1, 7):
        assert f"p{lv}" in out, f"缺少 p{lv} —— 这正是 V14-B1 要修的静默丢弃"
        assert f"p{lv}_amt" in out, f"缺少 p{lv}_amt"


def test_row_to_draw_legacy_keys_unchanged():
    """纯增量：原有 8 个键必须原样存在、类型不变。"""
    out = db._row_to_draw(_fake_row())
    assert out["issue"] == "2026999"
    assert out["reds"] == [1, 2, 3, 4, 5, 6]
    assert out["order"] == [7, 1, 2, 3, 4, 5]
    assert out["blue"] == 7
    assert out["sales"] == 123 and out["pool"] == 456
    assert out["source"] == "test"
    assert isinstance(out["reds"], list) and len(out["reds"]) == 6


def test_row_to_draw_tolerates_missing_prize_columns():
    """早期 schema 没有 p* 列时，应静默跳过而不是 KeyError。"""
    out = db._row_to_draw(_fake_row(with_prize=False))
    assert out["issue"] == "2026999"
    assert "p1" not in out, "缺列时不该凭空造键"
    assert "p1_amt" not in out


def test_row_to_draw_values_match_columns():
    """补上的键必须携带**正确值**，不能只是键存在。"""
    out = db._row_to_draw(_fake_row(with_prize=True))
    assert out["p1"] == 9
    assert out["p1_amt"] == 7232345
    assert out["p2"] == 73
    assert out["p6_amt"] == 5


# ---------- 2. 真值用例（真实库，执行期判定） ----------

def test_real_db_prize_fields_have_real_data():
    """真实库上 p1 / p1_amt 必须有大量非零值。

    实测基线：p1 非零 3266 期、p1_amt 非零 3269 期（共 3517 期）。
    用宽松下界（>3000）避免新开奖或补录造成误报。
    """
    _require_real_db()
    draws = db.load_draws()
    nz_p1 = sum(1 for d in draws if d.get("p1"))
    nz_amt = sum(1 for d in draws if d.get("p1_amt"))

    assert nz_p1 > 3000, f"p1 非零期数只有 {nz_p1}，应 >3000（实测基线 3266）"
    assert nz_amt > 3000, f"p1_amt 非零期数只有 {nz_amt}，应 >3000（实测基线 3269）"


def test_real_db_p1_zero_means_nobody_won():
    """`p1 == 0` 表示**无人中头奖**（实测 251 期），不是缺失值。

    这个区分很重要：若把 0 当缺失丢掉，会系统性高估平均中奖注数，
    进而把"热门形态"的效应算歪。

    ## 注意 `p1=0` 时 `p1_amt` 不一定是 0（实测）

    251 期里有 **3 期** `p1=0` 但 `p1_amt=5000000`：
    `2005132`、`2006018`、`2006100`。这是保底/滚存的历史记录特征 ——
    无人中奖时仍登记了保底单注金额，**不是数据错误，也不能按"缺失"处理**。
    因此这里只断言"绝大多数为零"，而不是"全部为零"。
    """
    _require_real_db()
    draws = db.load_draws()
    zeros = [d for d in draws if d.get("p1") == 0]
    assert len(zeros) > 100, f"实测应有约 251 期无人中头奖，实际 {len(zeros)}"

    nonzero_amt = [d for d in zeros if d.get("p1_amt")]
    assert len(nonzero_amt) <= 10, (
        f"p1=0 却有金额的期数异常增多：{len(nonzero_amt)} 期（实测基线 3 期）。"
        f"样例：{[(d['issue'], d['p1_amt']) for d in nonzero_amt[:5]]}"
    )
    # 那 3 期的金额应为保底值 500 万，而不是任意乱值
    for d in nonzero_amt:
        assert d["p1_amt"] == 5000000, (
            f"{d['issue']}: p1=0 时金额为 {d['p1_amt']}，实测历史值均为保底 5,000,000"
        )


def test_real_db_order_is_true_draw_order():
    """`order` 必须是真·开奖顺序，不是 `reds` 的副本。

    实测基线：3517/3517 期 `sorted(order) == sorted(reds)`，
    逐位完全相同的仅 8 期 —— 若变成全部相同，说明数据源退化了。
    """
    _require_real_db()
    draws = db.load_draws()
    same_set = sum(1 for d in draws if sorted(d["order"]) == sorted(d["reds"]))
    identical = sum(1 for d in draws if list(d["order"]) == list(d["reds"]))

    assert same_set == len(draws), "order 与 reds 必须是同一组号的两种排列"
    assert identical < len(draws) * 0.01, (
        f"逐位相同的期数 {identical}/{len(draws)} 过多 —— order 可能退化成了 reds 副本"
    )
