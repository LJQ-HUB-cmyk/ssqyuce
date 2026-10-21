"""统计内核（`lottery.stats`）：只做数学，不碰 IO、不碰 DB、不碰配置。

## 为什么要有这个包（V14-A，2026-10-21）

判定层的三个模块 —— `eprocess`（e-process + e-BH）、`rigor`（严格检验）——
本来就是**纯数学**的：它们的内部依赖数为 **0**（只用 `math`/`itertools`/`typing`）。
但它们一直散在 `lottery/` 根目录下，与 `engine.py`（3044 行）、`hypothesis.py`（1978 行）
等业务模块平铺混放，于是：

* 边界不可见 —— 谁都可以从「数学模块」里 import 业务代码，而没人会察觉；
* 测试放错位置 —— `ebh_selected` 的单元测试曾长在 `test_p4_candidate_space.py` 里，
  导致 `test_p4_eprocess.py` 的 17 条测试**全绿也抓不住 e-BH 被改坏**
  （变异测试实测：`k_star → k_star+1` 在该文件中存活）；
* 无法被独立复用 —— 想单独跑「这套 e-process 判定在一组合成数据上靠不靠谱」，
  就得把整个 `lottery` 包连带 `db`/`config` 一起拉进来。

本包把「数学」与「业务」在**目录层**上分开。这是架构审查员实测后推荐的路径：
`rigor.py` fanout=0、`eprocess.py` fanout=0、70 模块 0 循环依赖 ——
抽象本来就在，只是没有落地成包。

## 包内约定（新代码必须遵守）

1. **只允许标准库依赖**（`math`/`itertools`/`typing`/`functools`）。
   需要 numpy/scipy 时必须有纯 Python 回退，否则无法在最小环境里单测。
2. **不得 import `lottery` 下的任何业务模块**（`db`/`config`/`engine`/`discovery` …）。
   需要数据时由调用方以参数传入。
3. **不得读写文件、不读环境变量**。纯函数优先。
4. 纯数学模块的单测必须与实现放在同一文件（`test_<module>.py`），
   防止再出现「文件名叫 eprocess 却没测到 e-BH」。

## 兼容性

`lottery/eprocess.py` 与 `lottery/rigor.py` 已改为**再导出薄壳**，
所有既有的 `from . import eprocess` / `from lottery import rigor` 原样可用。
迁移是纯位移，没有语义改动。
"""
from __future__ import annotations

__all__ = ["eprocess", "rigor"]
