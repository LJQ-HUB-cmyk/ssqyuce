"""规律库域路由（规律列表 / 回测刷新）。

P2 模块化：从 `api_app.py` 切出。

## 这里的「规律」是 6 种含义中的第 2 种

M4 评审指出全站用「规律」一词讲了 **6 件判据完全不同的事**。本模块对应的是
「内置规律族的 A/B/C 档回测结果」——它回答的是"这些**预定义**规则在
walk-forward 样本外是否站得住"，而**不是**"系统发现了可预测下期的新规律"。

UI 上必须守住这个区别：A 档（显著）不等于"能用来预测"，
它在 3516 期真实数据上的结论是 **A=0**。这个"0"是诚实的结果，不是故障。
"""
from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter

from . import db

router = APIRouter()

@router.get("/api/patterns")
def get_patterns():
    return {
        "items": db.load_patterns(),
        "summary": _pattern_summary(db.load_patterns(grade_filter=None)),
    }


def _pattern_summary(patterns: List[Dict]) -> Dict:
    g = {"A": 0, "B": 0, "C": 0}
    for p in patterns:
        g[p.get("grade", "C")] = g.get(p.get("grade", "C"), 0) + 1
    return {"A": g["A"], "B": g["B"], "C": g["C"]}


@router.post("/api/patterns/backtest")
def run_backtests():
    from . import backtest as BT
    draws = db.load_draws()
    results = BT.run_all_backtests(draws, min_start=300)
    return {
        "items": db.load_patterns(),
        "summary": _pattern_summary(results),
        "note": ("walk-forward 样本外回测：只用目标期之前的历史，禁用未来信息。"
                 "A=显著，B=弱信号，C=不通过。"),
    }
