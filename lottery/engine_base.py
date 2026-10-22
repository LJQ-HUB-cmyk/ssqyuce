"""engine 系列子模块的**基础常量**（不含任何逻辑）。

## 为什么需要这个文件（D-02 拆分第 7 批踩到的循环导入）

拆分后子模块需要 `R_MAX`/`B_MAX`/`TICKET_COST`。若每个子模块都写
`from . import engine as E` 再取 `E.R_MAX`：
```
engine.py → from .engine_solver import ...
engine_solver → from . import engine as E     ← engine 此刻只初始化到一半
             → E.R_MAX 还不存在 → ImportError: partially initialized module
```
**把零逻辑常量下沉到这里，子模块直取，环就断了。**

## 边界

**只许放纯常量。** 任何带逻辑的东西都不要放 —— 否则会变成「第二个 engine」。
放常量的判据：不在 import 期产生副作用、不需要 monkeypatch 打桩。
"""

R_MAX = 33
B_MAX = 16
TICKET_COST = 2.0
