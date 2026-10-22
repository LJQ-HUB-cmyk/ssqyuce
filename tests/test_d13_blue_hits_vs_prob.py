"""D-13：`p_any_blue`（概率）与 `expected_blue_hits`（期望注数）不得混用。

## 背景

`blue_coverage.optimize()` 在 2026-10-21 修过一个缺陷：
`p_any_blue_covered`（「至少中一次」的**概率**）被当成**期望命中注数**乘了 ¥5，
n=100 时低估 **6.25 倍**（报 5.00 元，真实 31.25 元）。

`engine.ticket_distribution()` 当时**只给了概率、没给期望注数** ——
下游若需要期望，只能自己从概率推，而这正是那个缺陷的入口。

## 两个量的关系（必须都成立）

```
p_any_blue          = min(distinct_blue, 16) / 16    ← 概率，n>16 封顶 1.0
expected_blue_hits  = n / 16                          ← 期望注数，不封顶
```

n ≤ 16 时两者**数值相同**（都是 n/16）—— 这就是为什么「只在 n≤16 上测」
永远抓不到混用。**必须测 n > 16。**
"""
from __future__ import annotations

import pytest

from lottery import engine_distribution as ED


def _tickets(n: int) -> list[dict]:
    """n 注，蓝球在 1~16 内循环（覆盖 distinct_blue 封顶场景）。"""
    return [{"reds": [1, 2, 3, 4, 5, 6], "blue": (i % ED.B_MAX) + 1}
            for i in range(n)]


@pytest.mark.parametrize("n", [1, 10, 16, 17, 20, 100, 1000])
def test_expected_blue_hits_is_linear_in_n(n):
    """期望命中注数 = n/16，**与是否去重无关**，不封顶。"""
    out = ED.ticket_distribution(_tickets(n))
    assert out["expected_blue_hits"] == pytest.approx(n / 16.0, rel=1e-9), (
        f"n={n}: expected_blue_hits 应为 n/16={n/16}，实际 {out['expected_blue_hits']}")


@pytest.mark.parametrize("n", [1, 10, 16, 17, 20, 100, 1000])
def test_p_any_blue_caps_at_one(n):
    """`p_any_blue` 是概率，n ≥ 16 时封顶 1.0。"""
    out = ED.ticket_distribution(_tickets(n))
    assert out["p_any_blue"] == pytest.approx(min(n, 16) / 16.0, rel=1e-9)


@pytest.mark.parametrize("n", [17, 20, 100, 1000])
def test_two_quantities_diverge_above_16(n):
    """**n > 16 时两者必须不同** —— 这是本项存在的原因。

    若有人把 `expected_blue_hits` 误实现成 `p_any_blue` 的别名，
    本测试在 n>16 立刻变红。
    """
    out = ED.ticket_distribution(_tickets(n))
    p = out["p_any_blue"]
    e = out["expected_blue_hits"]
    assert p == pytest.approx(1.0), f"n={n}: 概率应封顶 1.0"
    assert e > 1.0, f"n={n}: 期望注数应 > 1.0（实际 {e}）"
    assert e > p, f"n={n}: 期望注数({e}) 必须大于概率({p}) —— 否则两者被混用了"


def test_sixth_prize_ev_uses_expected_hits_not_probability():
    """六等奖期望回收必须用 `expected_blue_hits`，不能用概率。

    n=100 时：正确 = 6.25 注 × 5 元 = **31.25 元**；
    错误（概率当注数）= 1.0 × 5 = **5.00 元**（低估 6.25 倍）。
    """
    out = ED.ticket_distribution(_tickets(100))
    hits = out["expected_blue_hits"]
    assert hits == pytest.approx(6.25), f"n=100 期望命中注数应为 6.25，实际 {hits}"

    # 若下游要算六等奖期望回收，必须用 hits
    sixth_yuan = hits * 5.0
    assert sixth_yuan == pytest.approx(31.25), (
        f"n=100 六等奖期望回收应为 31.25 元，实际 {sixth_yuan}"
        "（若得到 5.00，说明把概率当成了注数 —— 即 D-16 修复前那个缺陷形态）")
