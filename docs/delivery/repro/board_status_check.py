#!/usr/bin/env python3
"""看板状态 ⊆ 代码事实 核对器。

## 为什么需要（2026-10-21，严QA 第 4 次发现看板与代码不符）

| 次 | 看板说 | 代码事实 | 谁发现 |
|---|---|---|---|
| 1 | D-09/D-10 未开始 | `ee7560b` 已完成 | 严QA |
| 2 | `engine.py` 拆分进行中 | 早已回滚，`engine.py` 完好 | 严QA |
| 3 | D-03 的 D3 未落地 | `fillHonestBounds()` 已实现 | 齐交付 |
| 4 | **D-08 未开始** | **实现已完成（`ee7560b`+V14-A2）** | **严QA** |

**这是系统性缺口，不是偶发。** 人工核对漏一次就误导全员派工。

## 原理

对每个「未开始」的交付项，声明它的**关键符号**（必须在代码里出现的东西）。
若看板标「未开始」但符号已存在 ⇒ 报警。

## 用法

    .venv/bin/python docs/delivery/repro/board_status_check.py

退出码 0 = 无错位；1 = 有「标未开始但代码已有」的项。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BOARD = ROOT / "docs" / "delivery" / "BOARD.md"

# 交付项 → 该完成的标志符号（在代码里 grep）
# 只有「未开始」状态才检查；已完成/验收中的项跳过。
SIGNATURES = {
    "D-08": [
        # 缺陷①：m 查真源，不再用 135 常数
        ("lottery/idle_mining.py", r"_family_pairs\(\)\.get\(gkey\)"),
        # 缺陷②：分组键四段
        ("lottery/idle_mining.py", r"def _group_key_of"),
    ],
    "D-17": [
        # 两处静默 except 治理（尚未做，若出现即说明已完成）
        ("lottery/routes_capabilities.py", r"degraded.*except|except.*degraded"),
    ],
}

UNSTARTED = re.compile(r"\|\s*\*\*(D-\d+)\*\*.*?\|\s*(?:未开始|⬜\s*未开始)\s*\|")


def main() -> int:
    if not BOARD.exists():
        print(f"[SKIP] 看板不存在: {BOARD}", file=sys.stderr)
        return 0
    text = BOARD.read_text(encoding="utf-8")

    # 找出所有标「未开始」的项
    unstarted = {}
    for line in text.splitlines():
        if "未开始" not in line:
            continue
        m = re.search(r"\*\*(D-\d+)\*\*", line)
        if m:
            unstarted[m.group(1)] = line.strip()[:90]

    if not unstarted:
        print("✓ 无「未开始」项")
        return 0

    problems = []
    for item, _line in sorted(unstarted.items()):
        sigs = SIGNATURES.get(item)
        if not sigs:
            print(f"  {item}: 标「未开始」，未声明标志符号（跳过）")
            continue
        hits = []
        for rel, pat in sigs:
            f = ROOT / rel
            if not f.exists():
                continue
            if re.search(pat, f.read_text(encoding="utf-8")):
                hits.append(f"{rel}: /{pat}/")
        if hits:
            problems.append((item, hits))
            print(f"✗ {item}: 标「未开始」但代码已有标志")
            for h in hits:
                print(f"     命中 {h}")
        else:
            print(f"✓ {item}: 标「未开始」，代码确认无标志")

    if problems:
        print(f"\n✗ {len(problems)} 项看板状态与代码事实错位", file=sys.stderr)
        print("  请更新看板，或确认标志符号声明是否过宽", file=sys.stderr)
        return 1
    print("\n✓ 看板状态与代码事实一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
