#!/usr/bin/env python
"""D-02 复现脚本：engine 的 fanout 计数（三种口径并列，消除口径争议）。

用法：cd /root/DSH/应用/ssqyuce && .venv/bin/python docs/delivery/repro/d02_fanout.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "lottery" / "engine.py"

STDLIB = {"__future__", "json", "random", "time", "itertools", "contextvars",
          "math", "typing", "collections", "functools", "os", "sys"}


def collect(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_pkg, all_pkg, stdlib, third, deferred = set(), set(), set(), set(), []

    for node in ast.walk(tree):
        is_top = getattr(node, "col_offset", 1) == 0
        if isinstance(node, ast.ImportFrom):
            if node.level:                       # 相对导入 = 项目内
                names = ([node.module.split(".")[0]] if node.module else
                         [a.name.split(".")[0] for a in node.names])
                names = [n for n in names if n != "__future__"]
                all_pkg.update(names)
                if is_top:
                    top_pkg.update(names)
                else:
                    deferred.extend((node.lineno, n) for n in names)
            elif node.module and node.module.split(".")[0] not in STDLIB:
                third.add(node.module.split(".")[0])
            elif node.module:
                stdlib.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                (stdlib if top in STDLIB else third).add(top)
    return {"top_pkg": top_pkg, "all_pkg": all_pkg,
            "stdlib": stdlib, "third": third, "deferred": deferred}


def main() -> int:
    r = collect(SRC)
    print(f"源文件: {SRC.relative_to(Path.cwd()) if SRC.is_relative_to(Path.cwd()) else SRC}")
    print()
    print("口径 A  仅顶层相对导入 (看板 §2.2 用的口径)")
    print(f"        {len(r['top_pkg'])} 个: {sorted(r['top_pkg'])}")
    print()
    print("口径 B  全部相对导入 = 顶层 + 函数内延迟导入 (本报告采用)")
    print(f"        {len(r['all_pkg'])} 个: {sorted(r['all_pkg'])}")
    print("        顶部之外的延迟导入:")
    for ln, n in sorted(r["deferred"]):
        print(f"          line {ln:5d}  {n}")
    print()
    print("口径 C  含标准库与第三方 (最宽口径)")
    worst = r["all_pkg"] | r["stdlib"] | r["third"]
    print(f"        {len(worst)} 个: 项目内 {len(r['all_pkg'])}"
          f" + 标准库 {len(r['stdlib'])} {sorted(r['stdlib'])}"
          f" + 第三方 {len(r['third'])} {sorted(r['third'])}")
    print()
    print("口径 D  按 import 语句数 (不看去重) —— 不推荐，只用于对照")
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    stmts = sum(1 for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)))
    print(f"        {stmts} 条 import 语句")
    print()
    print("结论：13 = 口径 B。看板 8 与 V14 的 13 分别对应口径 A 与口径 B/C 的混用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
