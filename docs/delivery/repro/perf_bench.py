#!/usr/bin/env python3
"""性能测量标定基准（SERIES: load 不可比时用它归一化）。

## 为什么需要它

`load average` 是**排队长度**，不是**算力损失比例**：
4 核 load=12 与 64 核 load=12 含义完全不同。所以「它在我这跑 7 秒」
换台机器/换个时刻就不再是证据。

本脚本跑一段**固定工作量**的纯 CPU 计算，给出绝对耗时。
任何两次测量只要同时记录 `bench`，就能判断差异来自代码还是机器。

## 用法

    .venv/bin/python docs/delivery/repro/perf_bench.py

输出示例：

    load=12.51  bench=0.552s  (n=3,000,000)
    load= 0.42  bench=0.183s  (n=3,000,000)   ← 机器空下来后重测

**判据**：两次测量的 `bench` 相差 > 20% ⇒ 两者的性能数字不可直接比较，
必须用 `耗时 / bench` 归一化后再比。

## 纪律

报告里凡出现毫秒级以上的耗时，必须同时给 `load` 与 `bench`。
只给耗时不标环境 = 数字无意义（本项目 2026-10-21 事故记录）。
"""
from __future__ import annotations

import os
import statistics
import sys
import time

N = 20_000_000     # 工作量必须足够大：3M 时离散度可达 1.6x，无法区分负载与抖动
REPS = 5


def bench(n: int = N) -> list[float]:
    out = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        s = 0
        for i in range(n):
            s += i * i
        out.append(time.perf_counter() - t0)
    return out


def main() -> int:
    load = os.getloadavg()[0]
    nproc = os.cpu_count() or 1
    runs = bench()
    b = statistics.median(runs)
    spread = max(runs) / min(runs)
    print(f"load={load:5.2f}  nproc={nproc}  bench={b:.3f}s  "
          f"(min={min(runs):.3f} max={max(runs):.3f} spread={spread:.2f}x  n={N:,}×{REPS})")
    if spread > 1.5:
        print(f"[WARN] 同一进程内 5 次离散 {spread:.2f}x > 1.5 —— "
              f"机器抖动大，bench 本身不可信", file=sys.stderr)
    if load > nproc / 2:
        print(f"[WARN] 负载 {load:.2f} > nproc/2 ({nproc/2:.1f}) —— "
              f"本机性能测量不可直接作为验收依据", file=sys.stderr)
        print(f"[WARN] 建议：等负载下降后重测，或用 bench 归一化后再比较",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
