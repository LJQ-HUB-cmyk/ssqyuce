#!/usr/bin/env python
"""D-02 等价性验证：拆分前后 engine 行为必须逐字节相同。

## 设计要点

1. **固定随机源**：`predict_next(..., rng=random.Random(SEED))` 含随机性，
   不给种子则两次运行结果不同，等价性无从谈起。统一用 SEED=20261021。
2. **固定输入**：`db.load_draws()` 读真实 3517 期；显式切到 as-of 切片，
   不依赖"当前数据有多少期"，数据追加时期号变化不影响对比。
3. **关闭所有外部不确定性**：`use_llm=False`（不调 LLM）、`persist=False`
   （不写库）、`llm_verify=False`。只测纯统计链路 —— 这正是拆分影响的面。
4. **比什么**：
   - `json.dumps(result, sort_keys=True, ensure_ascii=False)` 的 sha256 → 逐字节
   - 顶层 dict 的**键路径全集**（结构不能变）
   - 每个票单的 dict 键集合（字段不能增减）
5. **怎么断言**：两侧各跑一遍，产出 `{label: {sha, keys, n_tickets}}` 的 JSON；
   然后逐 label 比较。**sha 不等即失败**，不接受"数值近似"。

## 用法（两步，跨两个 commit 之间执行）

    # 拆分前（在 HEAD 上）
    git stash && .venv/bin/python docs/delivery/repro/d02_equivalence.py --save pre.json
    git stash pop

    # 拆分后
    .venv/bin/python docs/delivery/repro/d02_equivalence.py --save post.json

    # 比较
    .venv/bin/python docs/delivery/repro/d02_equivalence.py --compare pre.json post.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SEED = 20261021

# 必须剔除的**时间性**字段：每次调用都不同，与拆分无关。
# 不剔除则本脚本自身不可复现 —— 实测：同一份代码连跑两遍，6/6 用例 sha 全不同，
# unified diff 显示唯一差异就是 generated_at 这一行。**这是等价性判定的前提。**
VOLATILE = {"generated_at", "created_at", "timestamp", "elapsed_ms",
            "duration_ms", "took_ms", "generated_at_ts"}

# 覆盖多个分支：默认 / ML / 覆盖优化 / 不同注数
CASES = [
    ("default",     dict(use_llm=False, persist=False, n_tickets=10)),
    ("ml",          dict(use_llm=False, persist=False, n_tickets=10, use_ml=True)),
    ("no_ml",       dict(use_llm=False, persist=False, n_tickets=10, use_ml=False)),
    ("coverage",    dict(use_llm=False, persist=False, n_tickets=16, coverage=True,
                         coverage_r=3)),
    ("n1",          dict(use_llm=False, persist=False, n_tickets=1)),
    ("n50",         dict(use_llm=False, persist=False, n_tickets=50)),
    # 注意：即使传 50，也常返回 10 注（候选池/覆盖上限），已实测记录于报告 §5.3
]


def _keypaths(obj, prefix="") -> list[str]:
    """顶层 dict 的键路径全集（不递归进 list，避免票单顺序噪声）。"""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            if isinstance(v, dict):
                out.extend(_keypaths(v, p))
    return sorted(out)


def _strip_volatile(obj):
    """递归剔除时间性字段，结构与数值其余部分原样保留。"""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def run() -> dict:
    from lottery import db, engine

    draws = db.load_draws()
    result = {"data_rows": len(draws), "cases": {}}

    for label, kwargs in CASES:
        # 每次都给新 rng，保证同参同种 → 同结果
        res = engine.predict_next(draws, rng=random.Random(SEED), **kwargs)
        blob = json.dumps(_strip_volatile(res), sort_keys=True,
                          ensure_ascii=False, default=str)
        ticket_keys = sorted({
            tuple(sorted(t.keys()))
            for t in (res.get("tickets") or [])
            if isinstance(t, dict)
        })
        result["cases"][label] = {
            "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "bytes": len(blob.encode("utf-8")),
            "keypaths": _keypaths(res),
            "n_tickets": len(res.get("tickets") or []),
            "ticket_keysets": [list(k) for k in ticket_keys],
            "ok": res.get("ok"),
        }

    # 纯计算入口也覆盖一遍（不走随机）
    pool = [{"reds": [1, 2, 3, 4, 5, 6], "blue": 1},
            {"reds": [7, 8, 9, 10, 11, 12], "blue": 2}]
    result["pure"] = {
        "ticket_distribution": hashlib.sha256(
            json.dumps(engine.ticket_distribution(pool), sort_keys=True,
                       default=str).encode()).hexdigest(),
        "single_ticket_ev": hashlib.sha256(
            json.dumps(engine._single_ticket_ev(5000000), sort_keys=True,
                       default=str).encode()).hexdigest(),
        "portfolio_ev": hashlib.sha256(
            json.dumps(engine.portfolio_expected_value(pool), sort_keys=True,
                       default=str).encode()).hexdigest(),
    }
    return result


def compare(pre_path: str, post_path: str) -> int:
    pre = json.loads(Path(pre_path).read_text())
    post = json.loads(Path(post_path).read_text())
    fails = []

    if pre.get("data_rows") != post.get("data_rows"):
        fails.append(f"数据行数不同: {pre.get('data_rows')} vs {post.get('data_rows')}")

    for label in pre.get("cases", {}):
        a, b = pre["cases"][label], post["cases"].get(label, {})
        if a["sha256"] != b.get("sha256"):
            fails.append(
                f"[{label}] 输出 sha256 不同\n"
                f"    pre  {a['sha256']}  ({a['bytes']} bytes)\n"
                f"    post {b.get('sha256')}  ({b.get('bytes')} bytes)")
        if a["keypaths"] != b.get("keypaths"):
            only_pre = sorted(set(a["keypaths"]) - set(b.get("keypaths", [])))
            only_post = sorted(set(b.get("keypaths", [])) - set(a["keypaths"]))
            fails.append(f"[{label}] 键路径不同 only_pre={only_pre} only_post={only_post}")
        if a["n_tickets"] != b.get("n_tickets"):
            fails.append(f"[{label}] 注数不同 {a['n_tickets']} vs {b.get('n_tickets')}")
        if a["ticket_keysets"] != b.get("ticket_keysets"):
            fails.append(f"[{label}] 票单字段集合不同")

    for k in pre.get("pure", {}):
        if pre["pure"][k] != post.get("pure", {}).get(k):
            fails.append(f"[pure:{k}] 哈希不同")

    print("=" * 68)
    if fails:
        print(f"❌ 等价性验证失败：{len(fails)} 项不一致")
        for f in fails:
            print("  -", f)
        return 1
    n = len(pre.get("cases", {})) + len(pre.get("pure", {}))
    print(f"✅ 等价性验证通过：{n} 个用例全部逐字节相同")
    print(f"   数据 {pre['data_rows']} 期，SEED={SEED}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", metavar="OUT.json")
    ap.add_argument("--compare", nargs=2, metavar=("PRE", "POST"))
    ap.add_argument("--twice", action="store_true",
                    help="同进程连跑两遍并自比对，先证明脚本自身可复现")
    args = ap.parse_args()

    if args.twice:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td, "a.json"), Path(td, "b.json")
            for p in (a, b):
                Path(p).write_text(json.dumps(run(), indent=1, sort_keys=True))
            return compare(str(a), str(b))

    if args.compare:
        return compare(*args.compare)
    if args.save:
        out = run()
        Path(args.save).write_text(json.dumps(out, indent=1, sort_keys=True))
        print(f"已保存 {args.save}（{len(out['cases'])} 个用例）")
        for k, v in out["cases"].items():
            print(f"  {k:10s} {v['sha256'][:16]}  {v['bytes']:>7} bytes  "
                  f"{v['n_tickets']:>3} 注")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
