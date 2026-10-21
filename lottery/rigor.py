"""兼容薄壳：实现已迁至 `lottery.stats.rigor`（V14-A，2026-10-21）。

## 为什么用「模块级代理」而不是简单镜像

迁移踩到两个真实的坑，都记在这里：

**坑 1：`import *` 不导出私有符号。**
第一版薄壳写 `from .stats.rigor import *`，而 `import *` 不导出下划线开头的名字，
导致 `selfcheck.py` 调 `rigor._valid_draws` 直接 AttributeError —— 一次挂了 16 个用例。

**坑 2：镜像会与真源"分家"。**
第二版把真源属性逐个复制进本模块命名空间。但这样就有了**两份状态**：
`monkeypatch.setattr(rigor, "randomness_report", fake)` 只改薄壳、不改真源，
而 `selfcheck.py` 内部是 `from . import rigor` 走薄壳、被测函数却又可能拿真源 ——
同一个名字在两个模块里指向不同对象，打桩期间断言必然翻车（全量跑才复现，
因为依赖测试顺序）。

## 解法：代理而非复制

把本模块的 `__getattr__` 指向真源，并在 `__setattr__` 时**同时写两边**。
这样：

* 读：`lottery.rigor.X` 永远等于 `lottery.stats.rigor.X`（单一真源）；
* 写：`monkeypatch.setattr(rigor, "X", fake)` 对真源同样生效，
  被测代码无论从哪个入口取 `X` 都拿到同一个对象；
* 私有符号（`_valid_draws` 等）走 `__getattr__` 自动可用，无需列举。

## 推荐用法

    from . import rigor             # 旧写法，仍然可用（走本模块代理）
    from .stats import rigor        # 新写法，推荐
"""
from __future__ import annotations

import sys as _sys
import types as _types

from .stats import rigor as _src

#: 本模块自有属性（代理逻辑本身），__setattr__ 需放行，不能转发给真源
_OWN = {"__name__", "__doc__", "__file__", "__package__", "__loader__",
        "__spec__", "__builtins__", "__all__"}


class _ProxyModule(_types.ModuleType):
    """把属性读写转发给真源模块的代理模块。"""

    def __getattr__(self, name):                 # 读：一律问真源
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            return getattr(_src, name)
        except AttributeError:
            raise AttributeError(
                f"module 'lottery.rigor' has no attribute {name!r}"
            ) from None

    def __setattr__(self, name, value):          # 写：同时写两边
        if name in _OWN:
            super().__setattr__(name, value)
            return
        setattr(_src, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _OWN:
            super().__delattr__(name)
            return
        if hasattr(_src, name):
            delattr(_src, name)
        if hasattr(type(self), name) or name in self.__dict__:
            super().__delattr__(name)


_sys.modules[__name__].__class__ = _ProxyModule

__all__ = [n for n in dir(_src) if not n.startswith("_")]
