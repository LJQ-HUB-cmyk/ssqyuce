#!/usr/bin/env python3
"""结构判据：**用户请求路径上不得可达任何 `fit()` 调用点**。

## 为什么要这个检查（D-16 事故记录）

D-16 修了三次，用户等待时间一次都没真正降下来：

```
修之前    ：两线程各 fit 一次      → 用户等 7s（自己那次）
加锁后    ：两线程排队等 1 次 fit  → 用户等 7s（别人那次）
加超时后  ：超时降级 BUT 拿到锁就自 fit → 用户等 9s（还是自己 fit）
```

每一版都消除了「上一版暴露的症状」，**没有一次触碰根因：用户路径上跑训练**。

## 为什么用 AST 而不是行为测试

行为测试（`assert elapsed < 3s`）依赖：
- 机器负载（load 12 时同一个函数 0.55s vs 0.14s）
- 超时阈值（太短 flaky，太长则缺陷在超时内完成，也"通过"）
- 缓存状态（`data/*model*.json` 是否存在）

**这些全是环境，不是性质。** AST 检查只看结构：请求路径可达的符号集里有没有 `fit`。
**与机器速度无关，不会因负载失效，也不会因调大 timeout 而假通过。**

## 用法

    .venv/bin/python docs/delivery/repro/no_fit_on_request_path.py

退出码 0 = 无违规；1 = 有请求路径可达 fit。
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# 用户请求会走到的入口。预热线程用的 `get_model()` 不在此列 —— 它**本来就该** fit。
REQUEST_ENTRIES = [
    "get_model_with_deadline",
    "portfolio_popularity",
]

# 视为「跑训练」的符号
FIT_SYMBOLS = {"fit", "_fit", "_fit_impl", "_refit", "_train"}

TARGET_FILES = [
    "lottery/popularity.py",
    "lottery/routes_portfolio.py",
]


def build_funcs(tree: ast.Module) -> dict[str, ast.AST]:
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
    return out


def callees(node: ast.AST) -> set[str]:
    out = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            f = x.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def check(entry: str, funcs: dict[str, ast.AST]) -> tuple[bool, list[str]]:
    """返回 (是否命中 fit, 到达 fit 的路径)。"""
    if entry not in funcs:
        return False, []
    seen: set[str] = set()
    # 栈元素：(符号, 到这里的路径)
    stack: list[tuple[str, list[str]]] = [(entry, [entry])]
    while stack:
        cur, path = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in FIT_SYMBOLS:
            return True, path
        if cur in funcs:
            for c in callees(funcs[cur]):
                if c not in seen:
                    stack.append((c, path + [c]))
    return False, []


def main() -> int:
    violations = []
    for rel in TARGET_FILES:
        p = ROOT / rel
        if not p.exists():
            print(f"[SKIP] {rel} 不存在", file=sys.stderr)
            continue
        funcs = build_funcs(ast.parse(io.open(p, encoding="utf-8").read()))
        for entry in REQUEST_ENTRIES:
            if entry not in funcs:
                continue
            hit, path = check(entry, funcs)
            flag = "VIOLATION" if hit else "ok"
            print(f"{flag:10s} {rel}::{entry:28s} 路径={' → '.join(path) if hit else '(无 fit)'}")
            if hit:
                violations.append((rel, entry, path))

    if violations:
        print(f"\n✗ {len(violations)} 处违规：请求路径可达 fit()", file=sys.stderr)
        print("  D-16 根因：用户路径上跑训练。修法见 docs/delivery/BOARD.md §12-I", file=sys.stderr)
        return 1
    print("\n✓ 请求路径无 fit 调用点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
